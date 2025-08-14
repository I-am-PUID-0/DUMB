from utils.config_loader import CONFIG_MANAGER as config
from utils.global_logger import logger, websocket_manager
from utils import user_management
from api.api_service import start_fastapi_process
from utils.processes import ProcessHandler
from utils.auto_update import Update
from utils.dependencies import initialize_dependencies
import subprocess, threading, time, tomllib


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

    initialize_dependencies(
        process_handler=process_handler,
        updater=updater,
        websocket_manager=websocket_manager,
        logger=logger,
    )

    try:
        user_management.create_system_user()
    except Exception as e:
        logger.error(f"An error occurred while creating system user: {e}")
        process_handler.shutdown(exit_code=1)

    if config.get("dumb", {}).get("api_service", {}).get("enabled"):
        start_fastapi_process()

    try:
        dumb_config = config.get("dumb", {})
        start_configured_process(dumb_config.get("frontend", {}), updater, "frontend")
    except Exception:
        process_handler.shutdown(exit_code=1)

    try:
        grouped_keys = [
            "zurg",
            "rclone",
            "decypharr",
            "plex",
            "jellyfin",
            "emby",
            "postgres",
            "pgadmin",
            "zilean",
            "plex_debrid",
            "phalanx_db",
            "cli_debrid",
            "cli_battery",
            "riven_backend",
            "riven_frontend",
            "radarr",
            "sonarr",
            "lidarr",
            "prowlarr",
            "whisparr",
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

    threading.Event().wait()


if __name__ == "__main__":
    main()
