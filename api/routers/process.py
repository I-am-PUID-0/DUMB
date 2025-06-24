from fastapi import APIRouter, HTTPException, Depends, Query
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
import json, copy, time


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
}

CORE_SERVICE_NAMES = {
    "riven_backend": "Riven",
    "cli_debrid": "CLID",
    "plex_debrid": "Plex Debrid",
    "decypharr": "Decypharr",
}

OPTIONAL_POST_CORE = ["riven_frontend"]


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
    if env is not None:
        # logger.debug(f"Checking for variables in service config. {env}")
        if any("{" in c for c in env):
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
        elif process:
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


@process_router.post("/stop-service")
async def stop_service(
    request: ServiceRequest,
    process_handler=Depends(get_process_handler),
    logger=Depends(get_logger),
    api_state=Depends(get_api_state),
):
    process_name = request.process_name
    logger.info(f"Received request to stop {process_name}")

    # Check if the service exists and is enabled
    # service_config = CONFIG_MANAGER.config.get(process_name)
    # logger.debug(f"Service config: {service_config}")
    # if not service_config or not service_config.get("enabled", False):
    #    raise HTTPException(status_code=404, detail="Service not enabled or found")

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


@process_router.post("/restart-service")
async def restart_service(
    request: ServiceRequest,
    process_handler=Depends(get_process_handler),
    updater=Depends(get_updater),
    logger=Depends(get_logger),
    api_state=Depends(get_api_state),
):
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
            raise HTTPException(status_code=500, detail=f"Failed to restart: {error}")

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
        if config_block.get(key) != value:
            logger.debug(
                f"Overriding '{key}' = '{value}' in service config for {config_block.get('process_name', 'Unknown Process')}"
            )
            config_block[key] = value
            updated = True
    if updated:
        CONFIG_MANAGER.save_config()


@process_router.post("/start-core-service")
async def start_core_services(
    request: UnifiedStartRequest,
    updater=Depends(get_updater),
    api_state=Depends(get_api_state),
    logger=Depends(get_logger),
):
    core_services = (
        [request.core_services]
        if isinstance(request.core_services, CoreServiceConfig)
        else request.core_services
    )

    optional_services = [svc.lower() for svc in request.optional_services or []]

    results = []
    errors = []

    for core_service in core_services:
        try:
            process_name = core_service.name
            debrid_service = (core_service.debrid_service or "").lower()
            debrid_key = core_service.debrid_key

            logger.info(f"Starting core service from process name: {process_name}")

            result = CONFIG_MANAGER.find_key_for_process(process_name)
            if result is None:
                raise HTTPException(
                    status_code=404, detail="Process not found in config"
                )

            config_key, instance_name = result
            logger.debug(
                f"Resolved process_name='{process_name}' to config_key='{config_key}', instance='{instance_name}'"
            )

            if config_key not in CORE_SERVICE_DEPENDENCIES:
                raise HTTPException(
                    status_code=400, detail=f"{process_name} is not a core service"
                )

            dependencies = CORE_SERVICE_DEPENDENCIES[config_key]
            logger.debug(f"Core service '{config_key}' requires: {dependencies}")

            full_config = CONFIG_MANAGER.config
            with open("/utils/dumb_config.json") as f:
                template_config = json.load(f)

            if any(svc in ["zilean", "pgadmin"] for svc in optional_services):
                if not full_config.get("postgres", {}).get("enabled"):
                    full_config["postgres"]["enabled"] = True
                    CONFIG_MANAGER.save_config()
                postgres_name = full_config["postgres"]["process_name"]
                if not wait_for_process_running(api_state, postgres_name):
                    logger.info(
                        f"Postgres service '{postgres_name}' is not running. Starting it now."
                    )
                    updater.auto_update(postgres_name, enable_update=False)
                    wait_for_process_running(api_state, postgres_name)

            for optional in optional_services:
                optional_key = optional.lower()
                if (
                    optional_key in full_config
                    and optional_key not in OPTIONAL_POST_CORE
                ):
                    opt_cfg = full_config[optional_key]
                    if not opt_cfg.get("enabled"):
                        opt_cfg["enabled"] = True
                        CONFIG_MANAGER.save_config()
                    apply_service_options(
                        opt_cfg,
                        core_service.service_options.get(optional_key, {}),
                        logger,
                    )
                    opt_process = opt_cfg["process_name"]
                    if not wait_for_process_running(api_state, opt_process):
                        updater.auto_update(
                            opt_process, enable_update=opt_cfg.get("auto_update", False)
                        )
                        wait_for_process_running(api_state, opt_process)

            for dep in dependencies:
                if dep in ["zurg", "rclone"]:
                    instances = full_config.get(dep, {}).get("instances", {})
                    instance = next(
                        (
                            name
                            for name, cfg in instances.items()
                            if cfg.get("core_service") == config_key
                        ),
                        None,
                    )

                    if instance:
                        instance_name = instance
                        instance_cfg = instances[instance]
                        if not instance_cfg.get("enabled"):
                            logger.info(
                                f"Enabling existing {dep} instance '{instance}'"
                            )
                            instance_cfg["enabled"] = True
                            CONFIG_MANAGER.save_config()
                        apply_service_options(
                            instance_cfg,
                            core_service.service_options.get(instance_name, {}),
                            logger,
                        )
                        if (
                            dep == "zurg"
                            and debrid_service == "realdebrid"
                            and debrid_key
                        ):
                            instance_cfg["api_key"] = debrid_key
                            CONFIG_MANAGER.save_config()
                    else:
                        logger.info(
                            f"No existing {dep} instance found for core service '{config_key}'"
                        )
                        base = template_config[dep]["instances"]["RealDebrid"]
                        instance_name = CORE_SERVICE_NAMES.get(
                            config_key, config_key.replace("_", " ").title()
                        )

                        if "RealDebrid" in instances and not instances[
                            "RealDebrid"
                        ].get("enabled"):
                            logger.info(
                                f"Removing unused default {dep} instance 'RealDebrid'"
                            )
                            del instances["RealDebrid"]

                        if dep == "zurg":
                            used_ports = {
                                cfg["port"]
                                for cfg in instances.values()
                                if "port" in cfg
                            }
                            port = 9090
                            while port in used_ports:
                                port += 1

                            new_instance_cfg = copy.deepcopy(base)
                            if debrid_service == "realdebrid" and debrid_key:
                                new_instance_cfg["api_key"] = debrid_key
                            new_instance_cfg.update(
                                {
                                    "enabled": True,
                                    "core_service": config_key,
                                    "process_name": f"{dep.capitalize()} w/ {instance_name}",
                                    "port": port,
                                    "config_dir": f"/{dep}/RD/{instance_name}",
                                    "config_file": f"/{dep}/RD/{instance_name}/config.yml",
                                    "command": f"/{dep}/RD/{instance_name}/{dep}",
                                }
                            )
                        elif dep == "rclone":
                            new_instance_cfg = copy.deepcopy(base)
                            rclone_cfg = {
                                "enabled": True,
                                "core_service": config_key,
                                "process_name": f"{dep.capitalize()} w/ {instance_name}",
                                "mount_name": f"{instance_name.lower()}",
                                "log_file": f"/log/rclone_w_{instance_name.lower()}.log",
                            }
                            if "zurg" in CORE_SERVICE_DEPENDENCIES.get(config_key, []):
                                rclone_cfg["zurg_enabled"] = True
                                rclone_cfg["decypharr_enabled"] = False
                                rclone_cfg["zurg_config_file"] = (
                                    f"/zurg/RD/{instance_name}/config.yml"
                                )
                            elif config_key == "decypharr":
                                rclone_cfg["zurg_enabled"] = False
                                rclone_cfg["decypharr_enabled"] = True
                                rclone_cfg["decypharr_config_file"] = ""
                            new_instance_cfg.update(rclone_cfg)

                        apply_service_options(
                            new_instance_cfg,
                            core_service.service_options.get(dep, {}),
                            logger,
                        )
                        full_config[dep]["instances"][instance_name] = new_instance_cfg
                        logger.info(
                            f"Created new {dep} instance for core service '{config_key}'"
                        )
                        CONFIG_MANAGER.save_config()
                        CONFIG_MANAGER.reload()
                        full_config = CONFIG_MANAGER.config

                    process_name = full_config[dep]["instances"][instance_name][
                        "process_name"
                    ]
                    auto_update_enabled = full_config[dep]["instances"][
                        instance_name
                    ].get("auto_update", False)
                    process, error = updater.auto_update(
                        process_name, enable_update=auto_update_enabled
                    )
                    if process and wait_for_process_running(api_state, process_name):
                        logger.info(f"{process_name} is confirmed running.")
                    else:
                        raise HTTPException(
                            status_code=500,
                            detail=f"{process_name} failed to start. {error or ''}".strip(),
                        )
                else:
                    dep_cfg = full_config.get(dep, {})
                    if not dep_cfg.get("enabled"):
                        logger.info(f"Enabling '{dep}' for core service '{config_key}'")
                        dep_cfg["enabled"] = True
                        CONFIG_MANAGER.save_config()
                    apply_service_options(
                        dep_cfg, core_service.service_options.get(dep, {}), logger
                    )
                    process_name = dep_cfg["process_name"]
                    auto_update_enabled = dep_cfg.get("auto_update", False)
                    if not wait_for_process_running(api_state, process_name):
                        logger.info(
                            f"{dep} service '{process_name}' is not running. Starting it now."
                        )
                        process, error = updater.auto_update(
                            process_name, enable_update=auto_update_enabled
                        )
                        if process and wait_for_process_running(
                            api_state, process_name
                        ):
                            logger.info(f"{process_name} is confirmed running.")
                        else:
                            raise HTTPException(
                                status_code=500,
                                detail=f"{process_name} failed to start. {error or ''}".strip(),
                            )

            core_cfg = full_config.get(config_key)
            if not core_cfg.get("enabled"):
                core_cfg["enabled"] = True
                CONFIG_MANAGER.save_config()
            apply_service_options(
                core_cfg, core_service.service_options.get(config_key, {}), logger
            )
            process_name = core_cfg["process_name"]
            auto_update_enabled = core_cfg.get("auto_update", False)
            if not wait_for_process_running(api_state, process_name):
                logger.info(
                    f"Core service '{process_name}' is not running. Starting it now."
                )
                process, error = updater.auto_update(
                    process_name, enable_update=auto_update_enabled
                )
                if process and wait_for_process_running(api_state, process_name):
                    logger.info(f"{process_name} is confirmed running.")
                else:
                    raise HTTPException(
                        status_code=500,
                        detail=f"{process_name} failed to start. {error or ''}".strip(),
                    )

                for optional in optional_services:
                    optional_key = optional.lower()
                    if (
                        optional_key in full_config
                        and optional_key in OPTIONAL_POST_CORE
                    ):
                        opt_cfg = full_config[optional_key]
                        if not opt_cfg.get("enabled"):
                            opt_cfg["enabled"] = True
                            CONFIG_MANAGER.save_config()
                        apply_service_options(
                            opt_cfg,
                            core_service.service_options.get(optional_key, {}),
                            logger,
                        )
                        opt_process = opt_cfg["process_name"]
                        if not wait_for_process_running(api_state, opt_process):
                            logger.info(
                                f"Optional service '{opt_process}' is not running. Starting it now."
                            )
                            updater.auto_update(
                                opt_process,
                                enable_update=opt_cfg.get("auto_update", False),
                            )
                            wait_for_process_running(api_state, opt_process)

                results.append({"service": core_service.name, "status": "started"})

        except HTTPException as e:
            errors.append({"service": core_service.name, "error": e.detail})

    return {"results": results, "errors": errors}
