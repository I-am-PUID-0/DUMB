from utils.config_loader import CONFIG_MANAGER
from utils.download import Downloader
from utils.global_logger import logger
from utils.versions import Versions
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
import os, re, shlex, shutil, tempfile, yaml
from ruamel.yaml import YAML

UI_SERVICE_DEFS = [
    {
        "name": "dumb_api_service",
        "config_key": "dumb",
        "subkey": "api_service",
        "path": "/scalar",
    },
    {"name": "cli_debrid", "config_key": "cli_debrid"},
    {"name": "cli_battery", "config_key": "cli_battery"},
    {"name": "decypharr", "config_key": "decypharr"},
    {"name": "nzbdav", "config_key": "nzbdav"},
    {
        "name": "emby",
        "config_key": "emby",
        "path": "/web/index.html",
        "path_prefix": "/web",
    },
    {
        "name": "jellyfin",
        "config_key": "jellyfin",
        "path": "/web/index.html",
        "path_prefix": "/web",
    },
    {"name": "lidarr", "config_key": "lidarr"},
    {"name": "plex", "config_key": "plex", "path": "/web/index.html"},
    {"name": "tautulli", "config_key": "tautulli"},
    {"name": "huntarr", "config_key": "huntarr"},
    {"name": "seerr", "config_key": "seerr"},
    {"name": "pgadmin4", "config_key": "pgadmin"},
    {"name": "prowlarr", "config_key": "prowlarr"},
    {"name": "riven_backend", "config_key": "riven_backend", "path": "/scalar"},
    {"name": "riven_frontend", "config_key": "riven_frontend"},
    {"name": "radarr", "config_key": "radarr"},
    {"name": "sonarr", "config_key": "sonarr"},
    {"name": "whisparr", "config_key": "whisparr"},
    {"name": "zilean", "config_key": "zilean"},
    {"name": "zurg", "config_key": "zurg"},
    {
        "name": "traefik",
        "config_key": "traefik",
        "path": "/dashboard/",
        "path_prefix": "/dashboard",
        "internal_service": "api@internal",
    },
]

_downloader = Downloader()
_versions = Versions()


def _get_traefik_config() -> Dict[str, Any]:
    return CONFIG_MANAGER.get("traefik") or {}


def get_traefik_config_dir() -> Path:
    config = _get_traefik_config()
    config_dir = config.get("config_dir")
    if config_dir:
        return Path(config_dir)
    config_file = config.get("config_file")
    if config_file:
        return Path(config_file).parent
    raise RuntimeError("Traefik config_dir/config_file is not configured.")


def get_traefik_bin() -> str:
    config = _get_traefik_config()
    command = config.get("command")
    if isinstance(command, str):
        command = shlex.split(command)
    if isinstance(command, list) and command:
        return command[0]
    raise RuntimeError("Traefik command is not configured.")


def get_traefik_config_file() -> Path:
    config = _get_traefik_config()
    config_file = config.get("config_file")
    if config_file:
        return Path(config_file)
    return get_traefik_config_dir() / "traefik.yml"


def _get_traefik_version_stamps() -> Tuple[str, str]:
    traefik_bin = get_traefik_bin()
    config_dir = get_traefik_config_dir()
    bin_stamp = os.path.join(os.path.dirname(traefik_bin), "VERSION")
    config_stamp = os.path.join(str(config_dir), "traefik.version")
    return bin_stamp, config_stamp


def _normalize_version(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None
    if not value.startswith("v"):
        value = f"v{value}"
    return value.lower()


def _parse_entrypoint_port(address: str, fallback: int) -> int:
    match = re.search(r":(\d+)$", str(address).strip())
    if not match:
        return fallback
    try:
        return int(match.group(1))
    except ValueError:
        return fallback


def ensure_traefik_config() -> None:
    """Ensure the Traefik config directory exists."""
    get_traefik_config_dir().mkdir(parents=True, exist_ok=True)


def _resolve_ui_service(service_def: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Resolve service definition to actual services. Returns a list to support multiple instances."""
    config_key = service_def["config_key"]
    subkey = service_def.get("subkey")
    config = CONFIG_MANAGER.config
    internal_service = service_def.get("internal_service")

    if config_key == "traefik":
        traefik_cfg = config.get("traefik", {})
        entrypoints = traefik_cfg.get("entrypoints", {})
        web_address = entrypoints.get("web", {}).get("address", ":18080")
        web_port = _parse_entrypoint_port(web_address, fallback=18080)
        return [{
            "name": service_def["name"],
            "process_name": "Traefik",
            "config_key": config_key,
            "host": "127.0.0.1",
            "port": web_port + 1,
            "path": service_def.get("path", ""),
            "path_prefix": service_def.get("path_prefix", ""),
            "internal_service": internal_service,
        }]

    if config_key == "dumb" and subkey:
        cfg = config.get("dumb", {}).get(subkey, {})
        if not cfg.get("enabled"):
            return []
        host = cfg.get("host", "127.0.0.1")
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return [{
            "name": service_def["name"],
            "process_name": cfg.get("process_name", service_def["name"]),
            "config_key": config_key,
            "host": host,
            "port": cfg.get("port"),
            "path": service_def.get("path", ""),
            "path_prefix": service_def.get("path_prefix", ""),
            "internal_service": internal_service,
        }]

    cfg = config.get(config_key, {})
    if not isinstance(cfg, dict):
        return []

    # Handle multiple instances
    if "instances" in cfg and isinstance(cfg["instances"], dict):
        services = []
        for instance_cfg in cfg["instances"].values():
            if instance_cfg.get("enabled") and "port" in instance_cfg:
                host = instance_cfg.get("host", cfg.get("host", "127.0.0.1"))
                if host in ("0.0.0.0", "::"):
                    host = "127.0.0.1"
                # Use process_name from instance config, which includes instance name
                process_name = instance_cfg.get("process_name", service_def["name"])
                services.append({
                    "name": process_name,  # Use full process name (e.g., "Sonarr NzbDAV")
                    "process_name": process_name,
                    "config_key": config_key,  # This is the service type (e.g., "sonarr")
                    "host": host,
                    "port": instance_cfg.get("port"),
                    "path": service_def.get("path", ""),
                    "path_prefix": service_def.get("path_prefix", ""),
                    "internal_service": internal_service,
                })
        return services

    # Handle single instance (no instances key)
    if not cfg.get("enabled"):
        return []

    host = cfg.get("host", "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    if config_key == "nzbdav":
        port = cfg.get("frontend_port")
        if not port:
            return []
    else:
        port = cfg.get("port")
        if not port:
            return []
    service = {
        "name": service_def["name"],
        "process_name": cfg.get("process_name", service_def["name"]),
        "config_key": config_key,
        "host": host,
        "port": port,
        "path": service_def.get("path", ""),
        "path_prefix": service_def.get("path_prefix", ""),
        "internal_service": internal_service,
    }
    if config_key == "nzbdav":
        direct_url_template = cfg.get("direct_url")
        if direct_url_template:
            service["direct_url"] = direct_url_template.format_map(
                {"frontend_port": port, "port": port}
            )
            service["direct_url_locked"] = True
        else:
            service["direct_url"] = f"http://{host}:{port}/"
    return [service]


def build_ui_services() -> List[Dict[str, Any]]:
    services = []
    for service_def in UI_SERVICE_DEFS:
        resolved_services = _resolve_ui_service(service_def)
        for resolved in resolved_services:
            if resolved.get("port"):
                services.append(resolved)
    return services


def _sanitize_service_name(name: str) -> str:
    """Sanitize service name for use in URLs and routing.

    Replaces spaces and forward slashes with underscores, then lowercases.
    This ensures the name is URL-safe and consistent across all uses.
    """
    return name.replace(" ", "_").replace("/", "_").lower()


def generate_traefik_config(services: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generate a Traefik dynamic configuration for detected services."""
    traefik_config = {"http": {"routers": {}, "services": {}, "middlewares": {}}}

    traefik_config["http"]["middlewares"]["ui_frame_headers"] = {
        "headers": {
            "customResponseHeaders": {
                "X-Frame-Options": "SAMEORIGIN",
                "Content-Security-Policy": "frame-ancestors 'self'",
            }
        }
    }

    for service in services:
        service_name = _sanitize_service_name(service["name"])
        host = service["host"]
        port = service["port"]
        path = service.get("path", "")
        path_prefix = service.get("path_prefix", "")
        internal_service = service.get("internal_service")

        router_name = f"{service_name}_router"
        service_entry = f"{service_name}_service"
        strip_middleware = f"{service_name}_strip"

        if not internal_service:
            traefik_config["http"]["services"][service_entry] = {
                "loadBalancer": {"servers": [{"url": f"http://{host}:{port}"}]}
            }

        root_path = f"/service/ui/{service_name}"

        traefik_config["http"]["middlewares"][strip_middleware] = {
            "stripPrefix": {"prefixes": [root_path]}
        }

        middlewares = [strip_middleware]
        add_prefix_middleware = None

        # Add the root path replacement first (before prefix middleware)
        # This ensures /$ gets replaced with /web/index.html before the prefix middleware runs
        if path:
            replace_middleware = f"{service_name}_replace"
            traefik_config["http"]["middlewares"][replace_middleware] = {
                "replacePathRegex": {"regex": r"^/?$", "replacement": path}
            }
            middlewares.append(replace_middleware)

        # For Emby/Jellyfin, add middleware to handle various paths
        if service_name in ("emby", "jellyfin") and path_prefix:
            # Handle /index.html -> /web/index.html
            index_redirect = f"{service_name}_index_redirect"
            traefik_config["http"]["middlewares"][index_redirect] = {
                "replacePathRegex": {"regex": r"^/index\.html", "replacement": f"{path_prefix}/index.html"}
            }
            middlewares.append(index_redirect)

            # Catch-all: prepend /web/ to any path that doesn't already start with /web/, /emby/, or /jellyfin/
            # We can't use negative lookahead (?!...) because Go regex doesn't support it
            # Instead, we'll use multiple middlewares to handle specific patterns

            # First, handle paths starting with /wizard/, /strings/, /scripts/, /lib/, /assets/, /fonts/, /images/, /css/, /js/
            # These are common Emby/Jellyfin directories that need /web/ prepended
            web_dirs = f"{service_name}_web_dirs"
            traefik_config["http"]["middlewares"][web_dirs] = {
                "replacePathRegex": {
                    "regex": r"^/(wizard|strings|scripts|lib|assets|fonts|images|css|js|sw\.js)(.*)",
                    "replacement": f"{path_prefix}/$1$2"
                }
            }
            middlewares.append(web_dirs)

        if path_prefix:
            replace_all_middleware = f"{service_name}_prefix"
            prefix_clean = path_prefix.strip("/")
            # Skip prefix middleware for services where path already handles routing
            # Emby/Jellyfin use path_prefix="/web" but path="/web/index.html" handles root
            # The negative lookahead regex isn't supported by Go regex engine
            if service_name in ("traefik", "emby", "jellyfin"):
                add_prefix_middleware = None
            else:
                regex = rf"^/(?!{prefix_clean}(?:/|$))(.*)"
                replacement = f"/{prefix_clean}/$1"
                traefik_config["http"]["middlewares"][replace_all_middleware] = {
                    "replacePathRegex": {"regex": regex, "replacement": replacement}
                }
                middlewares.append(replace_all_middleware)

        middlewares.append("ui_frame_headers")

        if service_name == "traefik":
            traefik_config["http"]["routers"][router_name] = {
                "entryPoints": ["web"],
                "rule": f"Path(`{root_path}`) || PathPrefix(`{root_path}/`)",
                "service": internal_service or service_entry,
                "middlewares": middlewares,
                "priority": 1,
            }

            api_router = f"{service_name}_api_router"
            dashboard_router = f"{service_name}_dashboard_router"
            base_middlewares = [strip_middleware, "ui_frame_headers"]
            if path:
                base_middlewares.append(replace_middleware)

            traefik_config["http"]["routers"][api_router] = {
                "entryPoints": ["web"],
                "rule": f"PathPrefix(`{root_path}/api`)",
                "service": internal_service or service_entry,
                "middlewares": base_middlewares,
                "priority": 1000,
            }
            traefik_config["http"]["routers"][dashboard_router] = {
                "entryPoints": ["web"],
                "rule": f"PathPrefix(`{root_path}/dashboard`)",
                "service": internal_service or service_entry,
                "middlewares": base_middlewares,
                "priority": 900,
            }

            assets_router = f"{service_name}_assets_router"
            assets_rewrite = f"{service_name}_assets_rewrite"
            traefik_config["http"]["middlewares"][assets_rewrite] = {
                "replacePathRegex": {
                    "regex": r"^/assets/(.*)",
                    "replacement": r"/dashboard/assets/$1",
                }
            }
            traefik_config["http"]["routers"][assets_router] = {
                "entryPoints": ["web"],
                "rule": f"PathPrefix(`{root_path}/assets/`)",
                "priority": 1000,
                "service": internal_service or service_entry,
                "middlewares": [strip_middleware, assets_rewrite, "ui_frame_headers"],
            }
        else:
            traefik_config["http"]["routers"][router_name] = {
                "entryPoints": ["web"],
                "rule": f"Path(`{root_path}`) || PathPrefix(`{root_path}/`)",
                "service": internal_service or service_entry,
                "middlewares": middlewares,
            }
            if service_name == "nzbdav":
                referer_router = f"{service_name}_referer_router"
                traefik_config["http"]["routers"][referer_router] = {
                    "entryPoints": ["web"],
                    "rule": (
                        # Use HeaderRegexp (not HeadersRegexp) - correct Traefik v2+ syntax
                        # Match requests with Referer header containing /ui/nzbdav
                        # And PathPrefix to catch root-relative requests from NzbDAV UI
                        f"HeaderRegexp(`Referer`, `.*\\/ui\\/{service_name}.*`) && "
                        "PathPrefix(`/`)"
                    ),
                    "priority": 2000,
                    "service": internal_service or service_entry,
                    "middlewares": middlewares,
                }

    return traefik_config


def write_traefik_config(path: Path, config: Dict[str, Any]) -> None:
    """Write Traefik config atomically to avoid empty/partial reads."""
    temp_path = path.with_suffix(path.suffix + ".tmp")
    yaml_safe = YAML(typ="safe")
    yaml_safe.default_flow_style = False
    with open(temp_path, "w") as file:
        yaml_safe.dump(config, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)


def ensure_ui_services_config(config_dir: str) -> List[Dict[str, Any]]:
    ensure_traefik_config()
    services = build_ui_services()
    legacy_path = Path(config_dir) / "services.json"
    if legacy_path.exists():
        legacy_path.unlink()
    traefik_config_path = Path(config_dir) / "services.yaml"
    write_traefik_config(traefik_config_path, generate_traefik_config(services))
    return services


def _read_traefik_version_stamp() -> Optional[str]:
    for path in _get_traefik_version_stamps():
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = handle.read().strip()
                    if data:
                        return data
            except Exception:
                continue
    return None


def _write_traefik_version_stamp(version: Optional[str]) -> None:
    if not version:
        return
    for path in _get_traefik_version_stamps():
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(str(version))
        except Exception:
            logger.debug("Failed to write Traefik version stamp to %s.", path)


def _current_traefik_version() -> Optional[str]:
    current, _ = _versions.version_check(process_name="Traefik", key="traefik")
    if current:
        return current
    return _read_traefik_version_stamp()


def _traefik_version_matches(version: Optional[str]) -> bool:
    if not version:
        return True
    normalized_target = _normalize_version(version)
    current = _current_traefik_version()
    if not current:
        return False
    return _normalize_version(current) == normalized_target


def _find_traefik_binary(root_dir: str) -> Optional[str]:
    for dirpath, _, filenames in os.walk(root_dir):
        if "traefik" in filenames:
            return os.path.join(dirpath, "traefik")
    return None


def _select_traefik_asset(
    assets: List[Dict[str, Any]], arch_tag: Optional[str]
) -> Optional[Dict[str, Any]]:
    if not assets:
        return None
    if arch_tag:
        for asset in assets:
            name = asset.get("name", "").lower()
            if arch_tag in name and name.endswith(".tar.gz"):
                return asset
        for asset in assets:
            name = asset.get("name", "").lower()
            if arch_tag in name:
                return asset
    return assets[0]


def _download_traefik_from_url(download_url: str, version: Optional[str]) -> None:
    logger.info("Downloading Traefik from %s", download_url)
    with tempfile.TemporaryDirectory() as temp_dir:
        success, error = _downloader.download_and_extract(download_url, temp_dir)
        if not success:
            raise RuntimeError(f"Failed to download Traefik: {error}")
        extracted_path = _find_traefik_binary(temp_dir)
        if not extracted_path:
            raise RuntimeError("Traefik binary not found in download.")
        shutil.copy2(extracted_path, get_traefik_bin())
    os.chmod(get_traefik_bin(), 0o755)
    _write_traefik_version_stamp(version)


def _download_traefik_release(version: str) -> None:
    release_info, error = _downloader.fetch_github_release_info(
        "traefik", "traefik", version
    )
    if error or not release_info:
        raise RuntimeError(error or "Failed to fetch Traefik release info.")

    arch_tag = _downloader.get_architecture().replace("-", "_")
    asset = _select_traefik_asset(release_info.get("assets", []), arch_tag)
    if not asset:
        raise RuntimeError("No Traefik release asset matched this platform.")

    download_url = asset.get("browser_download_url")
    if not download_url:
        raise RuntimeError("Traefik asset download URL missing.")

    _download_traefik_from_url(download_url, version)


def ensure_traefik_binary(version: Optional[str] = None) -> None:
    """Ensure the Traefik binary exists at the configured path."""
    normalized_version = _normalize_version(
        version or os.environ.get("TRAEFIK_VERSION", "v2.11.4")
    )
    traefik_bin = get_traefik_bin()
    if os.path.exists(traefik_bin) and os.access(traefik_bin, os.X_OK):
        if _traefik_version_matches(normalized_version):
            return

    os.makedirs(os.path.dirname(traefik_bin), exist_ok=True)

    download_url = os.environ.get("TRAEFIK_DOWNLOAD_URL")
    if download_url:
        _download_traefik_from_url(download_url, normalized_version)
        return

    if not normalized_version:
        raise RuntimeError("Traefik version not specified for download.")

    _download_traefik_release(normalized_version)


def setup_traefik(process_handler) -> Optional[tuple]:
    """Configures and starts Traefik using ProcessHandler."""
    traefik_config = _get_traefik_config()
    if not traefik_config or not traefik_config.get("enabled"):
        logger.info("Traefik is disabled. Skipping setup.")
        return True, None

    pinned_version = traefik_config.get("pinned_version") or traefik_config.get(
        "version"
    )
    if not _traefik_version_matches(pinned_version):
        process_name = traefik_config.get("process_name", "Traefik")
        if process_name in process_handler.process_names:
            process_handler.stop_process(process_name)
    ensure_traefik_binary(version=pinned_version)

    config_dir = str(get_traefik_config_dir())
    os.makedirs(config_dir, exist_ok=True)

    logger.info("Setting up Traefik configuration in %s", config_dir)

    entrypoints = traefik_config.get("entrypoints") or {"web": {"address": ":18080"}}
    web_address = entrypoints.get("web", {}).get("address", ":18080")
    web_port = _parse_entrypoint_port(web_address, fallback=18080)
    entrypoints["traefik"] = {"address": f":{web_port + 1}"}

    log_dir = "/log"
    os.makedirs(log_dir, exist_ok=True)
    log_level = (traefik_config.get("log_level") or "DEBUG").upper()
    log_file = traefik_config.get("log_file")
    access_log_file = traefik_config.get("access_log_file")
    log_config = {"level": log_level}
    if not log_file:
        # Keep Traefik writing to a file unless we manage rotation via subprocess logging.
        log_config["filePath"] = os.path.join(log_dir, "traefik.log")
    static_config = {
        "entryPoints": entrypoints,
        "api": {"dashboard": True, "insecure": True},
        "log": log_config,
        "accessLog": {
            "format": "json",
            "fields": {
                "names": {
                    "RouterName": "keep",
                    "ServiceName": "keep",
                    "EntryPointName": "keep",
                }
            },
        },
        "providers": {
            "file": {
                "filename": os.path.join(config_dir, "services.yaml"),
                "watch": True,
            }
        },
    }
    if not access_log_file:
        # Keep Traefik writing to a file unless we manage rotation via subprocess logging.
        static_config["accessLog"]["filePath"] = os.path.join(
            log_dir, "traefik_access.log"
        )

    static_config_path = str(get_traefik_config_file())
    with open(static_config_path, "w") as file:
        yaml.dump(static_config, file, default_flow_style=False)

    logger.info("Generated Traefik static config: %s", static_config_path)

    dynamic_config = {"http": {"routers": {}, "services": {}, "middlewares": {}}}
    has_dynamic = False

    if "middlewares" in traefik_config and traefik_config["middlewares"]:
        dynamic_config["http"]["middlewares"] = traefik_config["middlewares"]
        has_dynamic = True

    for service_name, service_info in traefik_config.get("services", {}).items():
        router_name = f"{service_name}_router"
        service_url = service_info.get("url")
        middlewares = service_info.get("middlewares", [])

        if not service_url:
            logger.warning("Skipping %s, no URL defined", service_name)
            continue

        dynamic_config["http"]["services"][service_name] = {
            "loadBalancer": {"servers": [{"url": service_url}]}
        }

        dynamic_config["http"]["routers"][router_name] = {
            "rule": f"PathPrefix(`/{service_name}`)",
            "service": service_name,
            "entryPoints": ["web"],
            "middlewares": middlewares,
        }
        has_dynamic = True

    dynamic_config_path = os.path.join(config_dir, "dynamic_config.yml")
    if has_dynamic:
        with open(dynamic_config_path, "w") as file:
            yaml.dump(dynamic_config, file, default_flow_style=False)
        logger.info("Generated Traefik dynamic config: %s", dynamic_config_path)
    elif os.path.exists(dynamic_config_path):
        os.remove(dynamic_config_path)

    ensure_ui_services_config(config_dir)

    return True, None
