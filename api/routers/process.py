from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from typing import Optional, List, Dict, Any, Union
from utils.dependencies import (
    get_process_handler,
    get_logger,
    get_api_state,
    get_updater,
)
from utils.config_loader import CONFIG_MANAGER, find_service_config
from utils.setup import setup_project
from utils.versions import Versions
import json, copy, time, glob


class ServiceRequest(BaseModel):
    process_name: str


class CoreServiceConfig(BaseModel):
    name: str
    debrid_service: Optional[str] = None
    debrid_key: Optional[str] = None
    service_options: Optional[Dict[str, Any]] = {}


class UnifiedStartRequest(BaseModel):
    core_services: Union[List[CoreServiceConfig], CoreServiceConfig]
    optional_services: Optional[List[str]] = []


process_router = APIRouter()
versions = Versions()

STATIC_URLS_BY_KEY = {
    "rclone": "https://rclone.org",
    "pgadmin": "https://www.pgadmin.org/",
    "postgres": "https://www.postgresql.org/",
    "dumb_api_service": "https://github.com/I-am-PUID-0/DUMB",
    "cli_battery": "https://github.com/godver3/cli_debrid/tree/main/cli_battery",
    "plex": "https://www.plex.tv/",
}

CORE_SERVICE_DEPENDENCIES = {
    "riven_backend": ["zurg", "rclone", "postgres"],
    "cli_debrid": ["zurg", "rclone", "cli_battery", "phalanx_db"],
    "plex_debrid": ["zurg", "rclone"],
    "decypharr": ["rclone"],
    "plex": [],
}

CORE_SERVICE_NAMES = {
    "riven_backend": "Riven",
    "cli_debrid": "CLID",
    "plex_debrid": "Plex Debrid",
    "decypharr": "Decypharr",
    "plex": "Plex Media Server",
}

CORE_SERVICE_DESCRIPTIONS = {
    "riven_backend": """\
Riven Backend Service
- Automates media collection, symlink creation, and metadata updates.
- Integrates with Overseerr, Plex, Trakt, and various scraper plugins (e.g. Torrentio, Jackett).

Documentation: https://i-am-puid-0.github.io/DUMB/services/core/riven-backend""",
    "cli_debrid": """\
CLI Debrid Service
- Lightweight, Python‑based downloader and streaming‑link creator.
- Integrates tightly with Real‑Debrid, Trakt, Plex, and various scraping services.
- Automates media collection, quality upgrades, and webhook‑driven triggers.
- Requires CLI Battery for metadata and optionally Phalanx DB for decentralized metadata.

Documentation: https://i-am-puid-0.github.io/DUMB/services/core/cli-debrid""",
    "plex_debrid": """\
Plex Debrid Service
- Not fully implemented yet, but intended for users with an existing Plex Debrid setup.
- Users will need to copy an existing Plex Debrid settings.json file to `.../plex_debrid` mount directory.

Documentation: https://i-am-puid-0.github.io/DUMB/services/core/plex-debrid""",
    "decypharr": """\
Decypharr Service
- Implementation of QbitTorrent with Multiple Debrid service support.
- Utilizes Sonarr and Radarr for media requests and management.
- Provides a WebDAV connection for easy access to media files.
- Integrates with Rclone for mounting of WebDAV content.

Documentation: https://i-am-puid-0.github.io/DUMB/services/core/decypharr""",
    "plex": """\
Plex Media Server
- Official Plex server for organizing, streaming, and sharing your media library.
- Exposes a full‑featured web UI on port 32400 (by default).
- Works seamlessly with the other DUMB services via shared mount paths.

Note: Automatic update is not supported for Plex Media Server at this time.
Recommended to run onboarding for Plex Media Server separately due to claim token timeout of 5 minutes.

Documentation: https://i-am-puid-0.github.io/DUMB/services/core/plex-media-server""",
}

OPTIONAL_POST_CORE = ["riven_frontend"]

OPTIONAL_SERVICES = {
    "zilean": "Zilean",
    "pgadmin": "PgAdmin",
    "postgres": "Postgres",
    "riven_frontend": "Riven Frontend",
}

OPTIONAL_SERVICES_DESCRIPTIONS = {
    "zilean": """\
Zilean
- Torznab‑compatible indexer and content discovery service.
- Enables users to search for debrid‑sourced content and share it via DUMB’s network.
- Can scrape from running Zurg instances or other Zilean peers.
- Configurable as an indexer in clients like Sonarr/Radarr.

Documentation: https://i-am-puid-0.github.io/DUMB/services/optional/zilean""",
    "pgadmin": """\
pgAdmin 4
- Web‑based administration tool for PostgreSQL databases.
- Pre‑installed and auto‑configured in DUMB for easy inspection, queries, and backups.
- Supports extensions like system_stats and pgAgent for advanced maintenance.

Documentation: https://i-am-puid-0.github.io/DUMB/services/optional/pgadmin""",
    "postgres": """\
PostgreSQL
- Core database system for storing metadata and internal configuration.
- Pre‑installed and initialized on container startup (default port 5432).
- Manages databases for pgAdmin, Zilean, and Riven by default.

Documentation: https://i-am-puid-0.github.io/DUMB/services/dependent/postgres""",
    "riven_frontend": """\
Riven Frontend
- Web UI for Riven Backend, providing a user‑friendly interface to manage and monitor services.
- Displays real‑time status of connected services, media libraries, and debrid providers.
- Allows users to trigger actions like metadata updates, link creation, and more.

Documentation: https://i-am-puid-0.github.io/DUMB/services/optional/riven-frontend""",
}

### create a list of debrid providers that are supported by each core service, and if any core service uses zurg as a dependency, then it is limited to RealDebrid.
CORE_SERVICE_DEBRID_PROVIDERS = {
    "riven_backend": ["RealDebrid"],
    "cli_debrid": ["RealDebrid"],
    "plex_debrid": ["RealDebrid"],
    "decypharr": ["RealDebrid"],
}

SERVICE_OPTION_DESCRIPTIONS = {
    "repo_owner": "GitHub username (owner) of the service repository.",
    "repo_name": "Name of the GitHub repository for the service.",
    "release_version_enabled": "Whether to pin to a specific release version.",
    "release_version": "The specific release tag or version to deploy.",
    "branch_enabled": "Whether to pin to a specific branch.",
    "branch": "The branch name to deploy.",
    "suppress_logging": "If true, silences all service log output.",
    "log_level": "Verbosity level for logs (e.g. DEBUG, INFO, WARN).",
    "port": "TCP port the service will listen on.",
    "auto_update": "Automatically check for new versions",
    "auto_update_interval": "Hours between automatic update checks.",
    "plex_claim": "Token used to claim the Plex Media Server. https://www.plex.tv/claim",
    "friendly_name": "A user-friendly name for the Plex Media Server.",
    "setup_email": "Email address pgAdmin4 login.",
    "setup_password": "Password for pgAdmin4 login.",
    "origin": "CORS origin for the service",
}

BASIC_FIELDS = set(SERVICE_OPTION_DESCRIPTIONS.keys())
ALIAS_TO_KEY = {v.lower(): k for k, v in CORE_SERVICE_NAMES.items()} | {
    v.lower(): k for k, v in OPTIONAL_SERVICES.items()
}


@process_router.get("/")
async def fetch_process(process_name: str = Query(...), logger=Depends(get_logger)):
    try:
        if not process_name:
            raise HTTPException(status_code=400, detail="process_name is required")

        config = find_service_config(CONFIG_MANAGER.config, process_name)
        if not config:
            raise HTTPException(status_code=404, detail="Process not found")

        config_key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
        version, _ = versions.version_check(
            process_name=config.get("process_name"),
            instance_name=instance_name,
            key=config_key,
        )

        return {
            "process_name": process_name,
            "config": config,
            "version": version,
            "config_key": config_key,
        }
    except Exception as e:
        logger.error(f"Failed to load process: {e}")
        raise HTTPException(status_code=500, detail="Failed to load process")


@process_router.get("/processes")
async def fetch_processes():
    logger = (Depends(get_logger),)
    try:
        processes = []
        config = CONFIG_MANAGER.config

        def find_processes(data, parent_key=""):
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, dict) and "process_name" in value:
                        process_name = value.get("process_name")
                        enabled = value.get("enabled", False)
                        display_name = f"{parent_key} {key}".strip()
                        config_key, instance_name = CONFIG_MANAGER.find_key_for_process(
                            process_name
                        )
                        version, _ = versions.version_check(
                            process_name=value.get("process_name"),
                            instance_name=instance_name,
                            key=config_key,
                        )
                        repo_owner = value.get("repo_owner")
                        repo_name = value.get("repo_name")
                        if repo_owner and repo_name:
                            repo_url = f"https://github.com/{repo_owner}/{repo_name}"
                        else:
                            repo_url = STATIC_URLS_BY_KEY.get(config_key)
                        processes.append(
                            {
                                "name": display_name,
                                "process_name": process_name,
                                "enabled": enabled,
                                "config": value,
                                "version": version,
                                "key": key,
                                "config_key": config_key,
                                "repo_url": repo_url,
                            }
                        )
                    elif isinstance(value, dict):
                        find_processes(value, parent_key=f"{parent_key} {key}".strip())

        find_processes(config)
        return {"processes": processes}
    except Exception as e:
        logger.error(f"Failed to load processes: {e}")
        raise HTTPException(status_code=500, detail="Failed to load processes")


@process_router.post("/start-service")
async def start_service(
    request: ServiceRequest,
    process_handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    logger=Depends(get_logger),
):
    def start():
        process_name = request.process_name
        service_config = find_service_config(CONFIG_MANAGER.config, process_name)

        if not service_config:
            raise HTTPException(status_code=404, detail="Service not enabled or found")

        if process_name in process_handler.setup_tracker:
            process_handler.setup_tracker.remove(process_name)
            success, error = setup_project(process_handler, process_name)
            if not success:
                raise HTTPException(
                    status_code=500, detail=f"Failed to setup project: {error}"
                )

        service_config["enabled"] = True
        command = service_config.get("command")
        if any("{" in c for c in command):
            success, error = setup_project(process_handler, process_name)
            if not success:
                raise HTTPException(
                    status_code=500, detail=f"Failed to setup project: {error}"
                )
            command = service_config.get("command")

        env = service_config.get("env")
        if env is not None and any("{" in c for c in env):
            success, error = setup_project(process_handler, process_name)
            if not success:
                raise HTTPException(
                    status_code=500, detail=f"Failed to setup project: {error}"
                )
            env = service_config.get("env")

        logger.info(f"Starting {process_name} with command: {command}")

        try:
            auto_update_enabled = service_config.get("auto_update", False)
            process, error = updater.auto_update(
                process_name, enable_update=auto_update_enabled
            )
            if not process:
                raise Exception(f"Error starting {process_name}: {error}")
            logger.info(f"{process_name} started successfully.")
            return {
                "status": "Service started successfully",
                "process_name": process_name,
            }
        except Exception as e:
            detailed_error = f"Service '{process_name}' could not be started due to an internal error: {str(e)}"
            logger.error(detailed_error)
            raise HTTPException(
                status_code=500,
                detail=f"Unable to start the service '{process_name}'. Please check the logs for more details.",
            )

    return await run_in_threadpool(start)


@process_router.post("/stop-service")
async def stop_service(
    request: ServiceRequest,
    process_handler=Depends(get_process_handler),
    logger=Depends(get_logger),
    api_state=Depends(get_api_state),
):
    def stop():
        process_name = request.process_name
        logger.info(f"Received request to stop {process_name}")

        if process_name in api_state.shutdown_in_progress:
            return {
                "status": "Shutdown already in progress",
                "process_name": process_name,
            }

        try:
            api_state.shutdown_in_progress.add(process_name)
            logger.debug(f"Shutdown in progress: {api_state.shutdown_in_progress}")
            process_handler.stop_process(process_name)
            logger.info(f"{process_name} stopped successfully.")
            return {
                "status": "Service stopped successfully",
                "process_name": process_name,
            }
        except Exception as e:
            logger.error(f"Failed to stop {process_name}: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to stop {process_name}: {str(e)}"
            )
        finally:
            api_state.shutdown_in_progress.remove(process_name)

    return await run_in_threadpool(stop)


@process_router.post("/restart-service")
async def restart_service(
    request: ServiceRequest,
    process_handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    logger=Depends(get_logger),
    api_state=Depends(get_api_state),
):
    def restart():
        process_name = request.process_name
        logger.info(f"Received request to restart {process_name}")

        try:
            process_handler.stop_process(process_name)
            logger.info(f"{process_name} stopped successfully.")

            service_config = find_service_config(CONFIG_MANAGER.config, process_name)
            if not service_config:
                raise HTTPException(
                    status_code=404, detail="Service configuration not found."
                )

            if process_name in process_handler.setup_tracker:
                process_handler.setup_tracker.remove(process_name)
                success, error = setup_project(process_handler, process_name)
                if not success:
                    raise HTTPException(
                        status_code=500, detail=f"Failed to setup project: {error}"
                    )

            auto_update_enabled = service_config.get("auto_update", False)
            process, error = updater.auto_update(
                process_name, enable_update=auto_update_enabled
            )
            if not process:
                raise HTTPException(
                    status_code=500, detail=f"Failed to restart: {error}"
                )

            logger.info(f"{process_name} started successfully.")

            status = api_state.get_status(process_name)
            if status != "running":
                raise HTTPException(
                    status_code=500,
                    detail=f"Service did not restart successfully. Current status: {status}",
                )

            return {
                "status": "Service restarted successfully",
                "process_name": process_name,
            }
        except Exception as e:
            logger.error(f"Failed to restart {process_name}: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to restart {process_name}: {str(e)}"
            )

    return await run_in_threadpool(restart)


@process_router.get("/service-status")
async def service_status(
    process_name: str = Query(..., description="The name of the process to check"),
    api_state=Depends(get_api_state),
):
    status = api_state.get_status(process_name)
    return {"process_name": process_name, "status": status}


def wait_for_process_running(
    api_state, process_name: str, timeout: int = 15, interval: float = 0.5
):
    start = time.time()
    while time.time() - start < timeout:
        status = api_state.get_status(process_name)
        if status == "running":
            return True
        time.sleep(interval)
    return False


def apply_service_options(config_block, options: dict, logger):
    updated = False
    for key, value in options.items():
        if value is None:
            continue
        if config_block.get(key) != value:
            logger.debug(
                f"Overriding '{key}' = '{value}' in service config for {config_block.get('process_name', 'Unknown Process')}"
            )
            config_block[key] = value
            updated = True
    if updated:
        CONFIG_MANAGER.save_config()


def normalize_identifier(identifier: str) -> str:
    ident = identifier.strip().lower()
    return ALIAS_TO_KEY.get(ident, ident)


@process_router.post("/start-core-service")
async def start_core_services(
    request: UnifiedStartRequest,
    updater=Depends(get_updater),
    api_state=Depends(get_api_state),
    logger=Depends(get_logger),
):
    outcome = await run_in_threadpool(_run_startup, request, updater, api_state, logger)
    return outcome


def _run_startup(request: UnifiedStartRequest, updater, api_state, logger):
    core_services = (
        [request.core_services]
        if isinstance(request.core_services, CoreServiceConfig)
        else request.core_services
    )
    raw_optionals = request.optional_services or []
    optional_services = [normalize_identifier(svc).lower() for svc in raw_optionals]

    results = []
    errors = []

    # Work in-place on the “single source of truth”
    config = CONFIG_MANAGER.config

    # Load template for creating new instances if needed
    with open("/utils/dumb_config.json") as f:
        template_config = json.load(f)

    for core_service in core_services:
        try:
            # Resolve config_key
            supplied = core_service.name
            ident = normalize_identifier(supplied)
            if ident in config:
                config_key, instance_name = ident, None
            else:
                config_key, instance_name = CONFIG_MANAGER.find_key_for_process(ident)
            if config_key is None:
                raise HTTPException(404, f"Process '{supplied}' not found")

            process_name = core_service.name
            debrid_service = (core_service.debrid_service or "").lower()
            debrid_key = core_service.debrid_key

            logger.info(f"Starting core service: {process_name}")
            logger.debug(f"→ config_key='{config_key}', instance='{instance_name}'")

            # Validate core service
            if config_key not in CORE_SERVICE_DEPENDENCIES:
                raise HTTPException(400, detail=f"{process_name} is not a core service")
            dependencies = CORE_SERVICE_DEPENDENCIES[config_key]
            logger.debug(f"Dependencies for '{config_key}': {dependencies}")

            #
            # 1) Pre-start “must-have” services (e.g. Postgres)
            #
            if any(s in ["zilean", "pgadmin"] for s in optional_services):
                pg = config["postgres"]
                if not pg.get("enabled"):
                    pg["enabled"] = True
                    CONFIG_MANAGER.save_config()
                pg_name = pg["process_name"]
                if not wait_for_process_running(api_state, pg_name):
                    logger.info(f"Starting Postgres '{pg_name}'…")
                    updater.auto_update(pg_name, enable_update=False)
                    wait_for_process_running(api_state, pg_name)

            #
            # 2) Start any “optional pre-core” services (those NOT in OPTIONAL_POST_CORE)
            #
            for opt in optional_services:
                if opt in config and opt not in OPTIONAL_POST_CORE:
                    opt_cfg = config[opt]
                    if not opt_cfg.get("enabled"):
                        opt_cfg["enabled"] = True
                        CONFIG_MANAGER.save_config()
                    apply_service_options(
                        opt_cfg,
                        core_service.service_options.get(opt, {}),
                        logger,
                    )
                    proc = opt_cfg["process_name"]
                    if not wait_for_process_running(api_state, proc):
                        updater.auto_update(
                            proc, enable_update=opt_cfg.get("auto_update", False)
                        )
                        wait_for_process_running(api_state, proc)

            #
            # 3) Handle zurg/rclone dependencies for this core service
            #
            for dep in dependencies:
                if dep in ("zurg", "rclone"):
                    instances = config[dep]["instances"]
                    # find existing instance for this core service
                    inst = next(
                        (
                            name
                            for name, c in instances.items()
                            if c.get("core_service") == config_key
                        ),
                        None,
                    )

                    if inst:
                        # enable + override
                        inst_cfg = instances[inst]
                        if not inst_cfg.get("enabled"):
                            inst_cfg["enabled"] = True
                            CONFIG_MANAGER.save_config()
                        apply_service_options(
                            inst_cfg,
                            core_service.service_options.get(inst, {}),
                            logger,
                        )
                        # special real-debrid API key injection
                        if (
                            dep == "zurg"
                            and debrid_service == "realdebrid"
                            and debrid_key
                        ):
                            inst_cfg["api_key"] = debrid_key
                            CONFIG_MANAGER.save_config()

                    else:
                        # no instance exists → clone from template, enable and override
                        base = template_config[dep]["instances"]["RealDebrid"]
                        display = CORE_SERVICE_NAMES.get(
                            config_key, config_key.replace("_", " ").title()
                        )
                        # clean up any disabled “RealDebrid” placeholder
                        if "RealDebrid" in instances and not instances[
                            "RealDebrid"
                        ].get("enabled"):
                            del instances["RealDebrid"]
                            CONFIG_MANAGER.save_config()

                        # build the new instance config
                        new_cfg = copy.deepcopy(base)
                        if dep == "zurg":
                            # allocate a free port
                            used = {
                                c["port"] for c in instances.values() if "port" in c
                            }
                            port = 9090
                            while port in used:
                                port += 1
                            new_cfg.update(
                                {
                                    "enabled": True,
                                    "core_service": config_key,
                                    "process_name": f"Zurg w/ {display}",
                                    "port": port,
                                    "config_dir": f"/zurg/RD/{display}",
                                    "config_file": f"/zurg/RD/{display}/config.yml",
                                    "command": f"/zurg/RD/{display}/zurg",
                                }
                            )
                            if debrid_service == "realdebrid" and debrid_key:
                                new_cfg["api_key"] = debrid_key

                        else:  # rclone
                            rclone_cfg = {
                                "enabled": True,
                                "core_service": config_key,
                                "process_name": f"Rclone w/ {display}",
                                "mount_name": display.lower(),
                                "log_file": f"/log/rclone_w_{display.lower()}.log",
                            }
                            # wire up zurg or decypharr flags
                            if "zurg" in dependencies:
                                rclone_cfg.update(
                                    {
                                        "zurg_enabled": True,
                                        "decypharr_enabled": False,
                                        "zurg_config_file": f"/zurg/RD/{display}/config.yml",
                                    }
                                )
                            elif config_key == "decypharr":
                                rclone_cfg.update(
                                    {
                                        "zurg_enabled": False,
                                        "decypharr_enabled": True,
                                        "zurg_config_file": "",
                                    }
                                )
                            new_cfg.update(rclone_cfg)

                        instances[display] = new_cfg
                        CONFIG_MANAGER.save_config()
                        apply_service_options(
                            instances[display],
                            core_service.service_options.get(dep, {}),
                            logger,
                        )

                    # start/update this instance
                    inst_name = inst or display
                    proc = config[dep]["instances"][inst_name]["process_name"]
                    ok = config[dep]["instances"][inst_name].get("auto_update", False)
                    proc_obj, err = updater.auto_update(proc, enable_update=ok)
                    if proc_obj and wait_for_process_running(api_state, proc):
                        logger.info(f"{proc} is running.")
                    else:
                        raise HTTPException(
                            500, detail=f"{proc} failed to start. {err or ''}"
                        )
                else:
                    # start any other dependencies that are not zurg/rclone and not in optional services or started already
                    dep_cfg = config.get(dep, {})
                    if not dep_cfg.get("enabled"):
                        dep_cfg["enabled"] = True
                        CONFIG_MANAGER.save_config()
                    apply_service_options(
                        dep_cfg,
                        core_service.service_options.get(dep, {}),
                        logger,
                    )
                    dep_proc = dep_cfg.get("process_name")
                    if not dep_proc:
                        raise HTTPException(
                            500, detail=f"Process name not defined for {dep}."
                        )
                    if not wait_for_process_running(api_state, dep_proc):
                        updater.auto_update(
                            dep_proc, enable_update=dep_cfg.get("auto_update", False)
                        )
                        if not wait_for_process_running(api_state, dep_proc):
                            raise HTTPException(
                                500,
                                detail=f"{dep_proc} failed to start. Please check the logs.",
                            )
                logger.debug(f"All dependencies for '{config_key}' are running.")
            #
            # 4) Finally, start the core service itself
            #
            core_cfg = config[config_key]
            if not core_cfg.get("enabled"):
                core_cfg["enabled"] = True
                CONFIG_MANAGER.save_config()

            if config_key == "decypharr" and debrid_service and debrid_key:
                core_cfg["debrid_service"] = debrid_service
                core_cfg["api_key"] = debrid_key
                CONFIG_MANAGER.save_config()

            apply_service_options(
                core_cfg,
                core_service.service_options.get(config_key, {}),
                logger,
            )

            proc_name = core_cfg["process_name"]
            auto_up = core_cfg.get("auto_update", False)
            if not wait_for_process_running(api_state, proc_name):
                p, err = updater.auto_update(proc_name, enable_update=auto_up)
                if not p or not wait_for_process_running(api_state, proc_name):
                    raise HTTPException(
                        500, detail=f"{proc_name} failed to start. {err or ''}"
                    )

            #
            # 5) Start any “optional post-core” services
            #
            for opt in optional_services:
                if opt in config and opt in OPTIONAL_POST_CORE:
                    oc = config[opt]
                    if not oc.get("enabled"):
                        oc["enabled"] = True
                        CONFIG_MANAGER.save_config()
                    apply_service_options(
                        oc,
                        core_service.service_options.get(opt, {}),
                        logger,
                    )
                    op = oc["process_name"]
                    if not wait_for_process_running(api_state, op):
                        updater.auto_update(
                            op, enable_update=oc.get("auto_update", False)
                        )
                        wait_for_process_running(api_state, op)

            results.append({"service": core_service.name, "status": "started"})

        except HTTPException as e:
            errors.append({"service": core_service.name, "error": e.detail})

    # Final persist & reload to ensure in-memory matches on-disk
    CONFIG_MANAGER.save_config()
    CONFIG_MANAGER.reload()

    logger.info("Core services started successfully.")
    return {"results": results, "errors": errors}


@process_router.get("/core-services")
async def get_core_services(logger=Depends(get_logger)):
    config_paths = glob.glob("/utils/*_config.json")
    if not config_paths:
        logger.error("No template config file found in /utils")
        raise HTTPException(status_code=500, detail="Template config file not found")
    if len(config_paths) > 1:
        logger.warning(
            "Multiple template config files found, using first: %s", config_paths
        )
    template_path = config_paths[0]
    with open(template_path) as f:
        default_conf = json.load(f)

    core_services = []
    for key, display_name in CORE_SERVICE_NAMES.items():
        desc = CORE_SERVICE_DESCRIPTIONS.get(key, "No description available")
        providers = CORE_SERVICE_DEBRID_PROVIDERS.get(key, [])
        if providers:
            desc += "\n\nSupported debrid providers: " + ", ".join(providers)

        svc_opts: Dict[str, Dict[str, Any]] = {}
        core_block = default_conf.get(key, {})
        svc_opts[key] = {k: v for k, v in core_block.items() if k in BASIC_FIELDS}

        for dep in CORE_SERVICE_DEPENDENCIES.get(key, []):
            if dep in ("zurg", "rclone"):
                instances = default_conf.get(dep, {}).get("instances", {})
                inst_cfg = next(
                    (
                        cfg
                        for cfg in instances.values()
                        if cfg.get("core_service") == key
                    ),
                    None,
                ) or next(iter(instances.values()), None)

                if inst_cfg:
                    svc_opts[dep] = {
                        k: v for k, v in inst_cfg.items() if k in BASIC_FIELDS
                    }

            else:
                dep_block = default_conf.get(dep, {})
                svc_opts[dep] = {
                    k: v for k, v in dep_block.items() if k in BASIC_FIELDS
                }

        svc_opt_desc: Dict[str, Dict[str, str]] = {}
        for svc_key, opts in svc_opts.items():
            svc_opt_desc[svc_key] = {
                field: SERVICE_OPTION_DESCRIPTIONS[field] for field in opts.keys()
            }

        core_services.append(
            {
                "name": display_name,
                "key": key,
                "dependencies": CORE_SERVICE_DEPENDENCIES.get(key, []),
                "description": desc,
                "debrid_providers": providers,
                "service_options": svc_opts,
                "service_option_descriptions": svc_opt_desc,
            }
        )

    return {"core_services": core_services}


@process_router.get("/optional-services")
async def get_optional_services(
    logger=Depends(get_logger),
    core_service: Optional[str] = Query(
        None, description="Key of chosen core service (to hide its dependencies)"
    ),
    optional_services: List[str] = Query(
        [],
        description="Already‑selected optional service keys (so we can drop postgres)",
    ),
):
    logger.debug(
        f"Fetching optional services for core_service={core_service!r} "
        f"with already-selected={optional_services}"
    )

    core_deps = (
        set(CORE_SERVICE_DEPENDENCIES.get(core_service, [])) if core_service else set()
    )
    picked = set(optional_services)

    config_paths = glob.glob("/utils/*_config.json")
    if not config_paths:
        logger.error("No template config file found in /utils")
        raise HTTPException(status_code=500, detail="Template config file not found")
    if len(config_paths) > 1:
        logger.warning(
            "Multiple template config files found, using first: %s", config_paths
        )
    template_path = config_paths[0]
    with open(template_path) as f:
        default_conf = json.load(f)

    results = []
    for (
        key,
        display_name,
    ) in OPTIONAL_SERVICES.items():
        if key in core_deps:
            continue
        if key == "postgres" and picked & {"zilean", "pgadmin"}:
            continue
        raw = default_conf.get(key, {})
        svc_opts = {
            k: raw[k]
            for k in SERVICE_OPTION_DESCRIPTIONS
            if k in raw and k in BASIC_FIELDS
        }
        svc_opt_desc = {
            field: SERVICE_OPTION_DESCRIPTIONS[field] for field in svc_opts.keys()
        }

        results.append(
            {
                "name": display_name,
                "key": key,
                "description": OPTIONAL_SERVICES_DESCRIPTIONS.get(
                    key, "No description available"
                ),
                "service_options": svc_opts,
                "service_option_descriptions": svc_opt_desc,
            }
        )

    return {"optional_services": results}
