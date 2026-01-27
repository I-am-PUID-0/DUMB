from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Dict, Any, Union
from utils.dependencies import (
    get_process_handler,
    get_logger,
    get_api_state,
    get_updater,
    get_optional_current_user,
)
from utils.config_loader import CONFIG_MANAGER, find_service_config
from utils.setup import setup_project
from utils.core_services import has_core_service
from utils.versions import Versions
import json, copy, time, glob, re, socket, errno, psutil


class ServiceRequest(BaseModel):
    process_name: str


class CoreServiceConfig(BaseModel):
    name: str
    instance_name: Optional[str] = None
    debrid_service: Optional[Union[str, List[str]]] = None
    debrid_key: Optional[Union[str, List[str]]] = None
    service_options: Optional[Dict[str, Any]] = {}
    model_config = ConfigDict(extra="forbid")


class UnifiedStartRequest(BaseModel):
    core_services: Union[List[CoreServiceConfig], CoreServiceConfig]
    optional_services: Optional[List[str]] = []
    optional_service_options: Optional[Dict[str, Dict[str, Any]]] = {}
    model_config = ConfigDict(extra="forbid")


process_router = APIRouter()
versions = Versions()

STATIC_URLS_BY_KEY = {
    "rclone": "https://rclone.org",
    "pgadmin": "https://www.pgadmin.org",
    "postgres": "https://www.postgresql.org",
    "dumb_api_service": "https://github.com/I-am-PUID-0/DUMB",
    "cli_battery": "https://github.com/godver3/cli_debrid/tree/main/cli_battery",
    "plex": "https://www.plex.tv/",
    "jellyfin": "https://jellyfin.org",
    "emby": "https://emby.media",
    "sonarr": "https://sonarr.tv/",
    "radarr": "https://radarr.video",
    "lidarr": "https://lidarr.audio",
    "bazarr": "https://www.bazarr.media",
    "prowlarr": "https://prowlarr.com",
    "readarr": "https://readarr.com",
    "whisparr": "https://whisparr.com",
    "whisparr-v3": "https://whisparr.com",
    "tautulli": "https://tautulli.com",
    "seerr": "https://github.com/seerr-team/seerr",
    "traefik": "https://traefik.io/",
    "huntarr": "https://plexguide.github.io/Huntarr.io/",
}

SPONSORSHIP_URLS_BY_KEY = {
    "rclone": "https://rclone.org/sponsor/",
    "pgadmin": "https://www.pgadmin.org/donate/",
    "postgres": "https://www.postgresql.org/about/donate/",
    "dumb_api_service": "https://github.com/sponsors/I-am-PUID-0",
    "cli_debrid": "https://github.com/sponsors/godver3",
    "cli_battery": "https://github.com/sponsors/godver3",
    "phalanx_db": "https://github.com/sponsors/godver3",
    "decypharr": "https://github.com/sponsors/sirrobot01",
    "plex": "https://www.plex.tv/plex-pass/",
    "jellyfin": "https://opencollective.com/jellyfin",
    "emby": "https://emby.media/premiere.html",
    "sonarr": "https://opencollective.com/sonarr",
    "radarr": "https://github.com/sponsors/Radarr",
    "riven_backend": "https://github.com/sponsors/dreulavelle/",
    "riven_frontend": "https://github.com/sponsors/dreulavelle/",
    "lidarr": "https://github.com/sponsors/Lidarr",
    "bazarr": "https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&hosted_button_id=XHHRWXT9YB7WE&source=url",
    "prowlarr": "https://github.com/sponsors/Prowlarr",
    "readarr": "https://readarr.com/donate",
    "whisparr": "https://opencollective.com/whisparr",
    "whisparr-v3": "https://opencollective.com/whisparr",
    "zurg": "https://github.com/sponsors/debridmediamanager",
    "tautulli": "https://tautulli.com/#donate",
    "seerr": "https://opencollective.com/seerr",
    "traefik": "https://github.com/sponsors/traefik",
    "huntarr": "https://plexguide.github.io/Huntarr.io/donate.html",
    "zilean": "https://ko-fi.com/W7W616IBNG",
}

DEFAULT_SERVICE_PORTS = {
    "radarr": 7878,
    "sonarr": 8989,
    "prowlarr": 9696,
    "lidarr": 8686,
    "whisparr": 6969,
    "bazarr": 6767,
    "jellyfin": 8096,
    "plex": 32400,
    "emby": 8096,
    "tautulli": 8181,
    "seerr": 5055,
    "huntarr": 9705,
}
## Future support for restricting service port ranges
SERVICE_PORT_RANGES = {
    # "radarr": (7800, 7999),
    # "sonarr": (8900, 9099),
    # ...
}

CORE_SERVICE_DEPENDENCIES = {
    "riven_backend": ["zurg", "rclone", "postgres"],
    "cli_debrid": ["zurg", "rclone", "cli_battery", "phalanx_db"],
    "plex_debrid": ["zurg", "rclone"],
    "decypharr": ["rclone"],
    "nzbdav": ["rclone"],
    "plex": [],
    "jellyfin": [],
    "emby": [],
    "sonarr": [],
    "radarr": [],
    "lidarr": [],
    "bazarr": [],
    "prowlarr": [],
    "whisparr": [],
    "seerr": [],
    "huntarr": [],
}

CORE_SERVICE_NAMES = {
    "plex": "Plex Media Server",
    "jellyfin": "Jellyfin Media Server",
    "emby": "Emby Media Server",
    "cli_debrid": "CLID",
    "decypharr": "Decypharr",
    "nzbdav": "NzbDAV",
    "riven_backend": "Riven",
    "radarr": "Radarr",
    "sonarr": "Sonarr",
    "lidarr": "Lidarr",
    "prowlarr": "Prowlarr",
    "whisparr": "Whisparr",
    "seerr": "Seerr",
    "huntarr": "Huntarr",
}

CORE_SERVICE_DESCRIPTIONS = {
    "riven_backend": """\
Riven Backend Service
- Automates media collection, symlink creation, and metadata updates.
- Integrates with Overseerr, Plex, Trakt, and various scraper plugins (e.g. Torrentio, Jackett).

Documentation: https://dumbarr.com/services/core/riven-backend""",
    "cli_debrid": """\
CLI Debrid Service
- Lightweight, Python‑based downloader and streaming‑link creator.
- Integrates tightly with Real‑Debrid, Trakt, Plex, and various scraping services.
- Automates media collection, quality upgrades, and webhook‑driven triggers.
- Requires CLI Battery for metadata and optionally Phalanx DB for decentralized metadata.

Documentation: https://dumbarr.com/services/core/cli-debrid""",
    "plex_debrid": """\
Plex Debrid Service
- Not fully implemented yet, but intended for users with an existing Plex Debrid setup.
- Users will need to copy an existing Plex Debrid settings.json file to `.../plex_debrid` mount directory.

Documentation: https://dumbarr.com/services/core/plex-debrid""",
    "decypharr": """\
Decypharr Service
- Implementation of QbitTorrent with Multiple Debrid service support.
- Utilizes Sonarr and Radarr for media requests and management.
- Provides a WebDAV connection for easy access to media files.
- Integrates with Rclone for mounting of WebDAV content.

Documentation: https://dumbarr.com/services/core/decypharr""",
    "nzbdav": """\
NzbDAV Service
- Implementation of QbitTorrent with Multiple NZB provider service support.
- Utilizes Sonarr and Radarr for media requests and management.
- Provides a WebDAV connection for easy access to media files.
- Integrates with Rclone for mounting of WebDAV content.

Documentation: https://dumbarr.com/services/core/nzbdav/""",
    "plex": """\
Plex Media Server
- Official Plex server for organizing, streaming, and sharing your media library.
- Exposes a full‑featured web UI on port 32400 (by default).
- Works seamlessly with the other DUMB services via shared mount paths.
- By enabling Plex, you confirm that you have read and agree to the Plex Terms of Service: https://www.plex.tv/about/privacy-legal/plex-terms-of-service/

Recommended to run onboarding for Plex Media Server separately due to claim token timeout of 5 minutes.

Documentation: https://dumbarr.com/services/core/plex-media-server""",
    "jellyfin": """\
Jellyfin Media Server
- Open‑source media server software for organizing and streaming your media library.
- Provides a web interface for managing and accessing your media.
- Supports a wide range of media formats and devices.
- Can be used as an alternative to Plex for users who prefer open‑source solutions.

Documentation: https://dumbarr.com/services/core/jellyfin""",
    "emby": """\
Emby Media Server
- Media server software for organizing and streaming your media library.
- Provides a web interface for managing and accessing your media.
- Supports a wide range of media formats and devices.
- Can be used as an alternative to Plex and Jellyfin for users who prefer Emby.
- By enabling Emby, you confirm that you have read and agree to the Emby Terms of Service: https://emby.media/terms.html

Documentation: https://dumbarr.com/services/core/emby""",
    "sonarr": """\
Sonarr
- TV series management and automation tool.
- Monitors RSS feeds for new episodes and automatically downloads them.
- Integrates with various download clients and indexers.
- Organizes and renames downloaded episodes for easy access.
- Works seamlessly with Radarr, Lidarr, and other media management tools.
- Supports multiple instances for different user profiles or libraries.

Documentation: https://dumbarr.com/services/core/sonarr""",
    "radarr": """\
Radarr
- Movie management and automation tool.
- Monitors RSS feeds for new movie releases and automatically downloads them.
- Integrates with various download clients and indexers.
- Organizes and renames downloaded movies for easy access.
- Works seamlessly with Sonarr, Lidarr, and other media management tools.
- Supports multiple instances for different user profiles or libraries.

Documentation: https://dumbarr.com/services/core/radarr""",
    "lidarr": """\
Lidarr
- Music management and automation tool.
- Monitors RSS feeds for new album releases and automatically downloads them.
- Integrates with various download clients and indexers.
- Organizes and renames downloaded music for easy access.
- Works seamlessly with Sonarr, Radarr, and other media management tools.
- Supports multiple instances for different user profiles or libraries.

Documentation: https://dumbarr.com/services/core/lidarr""",
    "bazarr": """\
Bazarr
- Subtitle management and automation tool.
- Monitors your media library and automatically downloads subtitles.
- Integrates with Sonarr, Radarr, and Lidarr for seamless subtitle management.
- Supports multiple subtitle providers and languages.

Documentation: https://dumbarr.com/services/core/bazarr""",
    "prowlarr": """\
Prowlarr
- Indexer manager and proxy for Sonarr, Radarr, Lidarr, and other media management tools.
- Centralizes the management of indexers for easier configuration and maintenance.
- Supports both torrent and usenet indexers.
- Provides a unified interface for searching and managing indexers.
- Works seamlessly with Sonarr, Radarr, Lidarr, and other media management tools.
- Supports multiple instances for different user profiles or libraries.
- Can scrape from Zilean.

Documentation: https://dumbarr.com/services/core/prowlarr""",
    "whisparr": """\
Whisparr
- Adult content management and automation tool.
- Monitors RSS feeds for new adult content releases and automatically downloads them.
- Integrates with various download clients and indexers.
- Organizes and renames downloaded adult content for easy access.
- Works seamlessly with Sonarr, Radarr, Lidarr, and other media management tools.
- Supports multiple instances for different user profiles or libraries.

Documentation: https://dumbarr.com/services/core/whisparr""",
    "seerr": """\
Seerr
- Media request management tool for Plex, Jellyfin, and Emby.
- Provides a web UI for requesting, approving, and tracking content.
- Integrates with Sonarr and Radarr for automated downloads.

Documentation: https://dumbarr.com/services/core/seerr/""",
    "huntarr": """\
Huntarr
- Continuously scans Sonarr/Radarr/Lidarr/Whisparr libraries for missing items and upgrades.
- Automates backlog searches in gentle batches to avoid indexer abuse.
- Supports multiple instances and per-arr configuration.

Documentation: https://dumbarr.com/services/core/huntarr/""",
}

OPTIONAL_POST_CORE = ["riven_frontend"]

OPTIONAL_SERVICES = {
    "zilean": "Zilean",
    "pgadmin": "PgAdmin",
    "postgres": "Postgres",
    "riven_frontend": "Riven Frontend",
    "tautulli": "Tautulli",
}

OPTIONAL_SERVICES_DESCRIPTIONS = {
    "zilean": """\
Zilean
- Torznab‑compatible indexer and content discovery service.
- Enables users to search for debrid‑sourced content and share it via DUMB’s network.
- Can scrape from running Zurg instances or other Zilean peers.
- Configurable as an indexer in clients like Sonarr/Radarr.

Documentation: https://dumbarr.com/services/optional/zilean""",
    "pgadmin": """\
pgAdmin 4
- Web‑based administration tool for PostgreSQL databases.
- Pre‑installed and auto‑configured in DUMB for easy inspection, queries, and backups.
- Supports extensions like system_stats and pgAgent for advanced maintenance.

Documentation: https://dumbarr.com/services/optional/pgadmin""",
    "postgres": """\
PostgreSQL
- Core database system for storing metadata and internal configuration.
- Pre‑installed and initialized on container startup (default port 5432).
- Manages databases for pgAdmin, Zilean, and Riven by default.

Documentation: https://dumbarr.com/services/dependent/postgres""",
    "riven_frontend": """\
Riven Frontend
- Web UI for Riven Backend, providing a user‑friendly interface to manage and monitor services.
- Displays real‑time status of connected services, media libraries, and debrid providers.
- Allows users to trigger actions like metadata updates, link creation, and more.

Documentation: https://dumbarr.com/services/optional/riven-frontend""",
    "tautulli": """\
Tautulli
- Plex monitoring and analytics tool.
- Tracks stream activity, history, and user stats.
- Provides alerts, newsletters, and watch history insights.

Documentation: https://github.com/Tautulli/Tautulli""",
}

### create a list of debrid providers that are supported by each core service, and if any core service uses zurg as a dependency, then it is limited to RealDebrid.
CORE_SERVICE_DEBRID_PROVIDERS = {
    "riven_backend": ["RealDebrid"],
    "cli_debrid": ["RealDebrid"],
    "plex_debrid": ["RealDebrid"],
    "decypharr": ["RealDebrid", "AllDebrid", "Debrid Link", "TorBox"],
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
    "frontend_port": "TCP port the NzbDAV frontend will listen on.",
    "backend_port": "TCP port the NzbDAV backend will listen on.",
    "auto_update": "Automatically check for new versions",
    "auto_update_interval": "Hours between automatic update checks.",
    "plex_claim": "Token used to claim the Plex Media Server. https://www.plex.tv/claim",
    "friendly_name": "A user-friendly name for the Plex Media Server.",
    "setup_email": "Email address pgAdmin4 login.",
    "setup_password": "Password for pgAdmin4 login.",
    "origin": "CORS origin for the service",
    "use_embedded_rclone": "If true, uses the embedded rclone for Decypharr. (Recommended)",
    "use_huntarr": "If true, auto-configures Huntarr for this Arr instance.",
    "core_service": "Specifies which core service(s) this service applies to; e.g., decypharr, nzbdav, both (decypharr,nzbdav), or none (blank).",
    "webdav_password": "Password for accessing the NzbDAV WebDAV service. Leave blank to auto-generate.",
}

BASIC_FIELDS = set(SERVICE_OPTION_DESCRIPTIONS.keys())
ALIAS_TO_KEY = {v.lower(): k for k, v in CORE_SERVICE_NAMES.items()} | {
    v.lower(): k for k, v in OPTIONAL_SERVICES.items()
}


@process_router.get("/")
def fetch_process(
    process_name: str = Query(...),
    logger=Depends(get_logger),
    current_user: str = Depends(get_optional_current_user),
):
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
def fetch_processes(
    logger=Depends(get_logger), current_user: str = Depends(get_optional_current_user)
):
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
                        sponsorship_url = SPONSORSHIP_URLS_BY_KEY.get(config_key)
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
                                "sponsorship_url": sponsorship_url,
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
    current_user: str = Depends(get_optional_current_user),
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

            key, _ = CONFIG_MANAGER.find_key_for_process(process_name)
            if key in [
                "prowlarr",
                "sonarr",
                "radarr",
                "lidarr",
                "readarr",
                "whisparr",
                "whisparr-v3",
            ]:
                try:
                    from utils.prowlarr_settings import patch_prowlarr_apps

                    ok, err = patch_prowlarr_apps()
                    if not ok and err:
                        logger.warning("Prowlarr app sync failed: %s", err)
                except Exception as e:
                    logger.warning("Prowlarr app sync skipped: %s", e)
            if key in [
                "huntarr",
                "sonarr",
                "radarr",
                "lidarr",
                "whisparr",
            ]:
                try:
                    from utils.huntarr_settings import (
                        any_arr_uses_huntarr,
                        patch_huntarr_config,
                    )

                    if key == "huntarr" or any_arr_uses_huntarr():
                        ok, err = patch_huntarr_config()
                        if not ok and err:
                            logger.warning("Huntarr config sync failed: %s", err)
                except Exception as e:
                    logger.warning("Huntarr config sync skipped: %s", e)
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
    current_user: str = Depends(get_optional_current_user),
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
    current_user: str = Depends(get_optional_current_user),
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

            key, _ = CONFIG_MANAGER.find_key_for_process(process_name)
            if key in [
                "prowlarr",
                "sonarr",
                "radarr",
                "lidarr",
                "readarr",
                "whisparr",
                "whisparr-v3",
            ]:
                try:
                    from utils.prowlarr_settings import patch_prowlarr_apps

                    ok, err = patch_prowlarr_apps()
                    if not ok and err:
                        logger.warning("Prowlarr app sync failed: %s", err)
                except Exception as e:
                    logger.warning("Prowlarr app sync skipped: %s", e)
            if key in [
                "huntarr",
                "sonarr",
                "radarr",
                "lidarr",
                "whisparr",
            ]:
                try:
                    from utils.huntarr_settings import (
                        any_arr_uses_huntarr,
                        patch_huntarr_config,
                    )

                    if key == "huntarr" or any_arr_uses_huntarr():
                        ok, err = patch_huntarr_config()
                        if not ok and err:
                            logger.warning("Huntarr config sync failed: %s", err)
                except Exception as e:
                    logger.warning("Huntarr config sync skipped: %s", e)

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
def service_status(
    process_name: str = Query(..., description="The name of the process to check"),
    include_health: bool = Query(
        False, description="If true, include health checks for the process"
    ),
    api_state=Depends(get_api_state),
    current_user: str = Depends(get_optional_current_user),
):
    details = api_state.get_status_details(process_name, include_health=include_health)
    response = {"process_name": process_name, **details}
    return response


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


def normalize_instance_name(instance_name: str) -> tuple[str, str]:
    """
    Returns (cleaned_display, cleaned_path)
    - display: letters/numbers/space/_/- only; trimmed
    - path: lowercase, spaces->underscores, no special chars
    """
    cleaned_display = re.sub(r"[^A-Za-z0-9 _-]", "", instance_name or "").strip()
    cleaned_path = cleaned_display.lower().replace(" ", "_")
    return cleaned_display, cleaned_path


def _purge_template_placeholders(
    instances: dict, tmpl_instances: dict, keep: str | None = None
) -> bool:
    """
    Remove any template/default instance names present in current config.
    If `keep` matches one of them, that one is spared; all others are removed.
    Returns True if mutated the instances.
    """
    if not isinstance(instances, dict) or not isinstance(tmpl_instances, dict):
        return False
    changed = False
    template_names = set(tmpl_instances.keys())
    for name in list(instances.keys()):
        if name in template_names and name != keep:
            del instances[name]
            changed = True
    if changed:
        CONFIG_MANAGER.save_config()
    return changed


def _rewrite_paths_for_instance(cfg: dict, service_key: str, path_segment: str) -> None:
    """
    Rewrites common path fields to use '/{service_key}/{path_segment}' instead of '/{service_key}/default'.
    Also replaces '/default/' occurrences inside values for safety.
    """
    if not isinstance(cfg, dict):
        return

    def _rewrite(value: str) -> str:
        if not isinstance(value, str):
            return value
        base = f"/{service_key}/default"
        tgt = f"/{service_key}/{path_segment}"
        v = value.replace(base, tgt)
        v = v.replace("/default/", f"/{path_segment}/")
        return v

    for k in ("config_dir", "config_file", "log_file"):
        if k in cfg:
            cfg[k] = _rewrite(cfg[k])


def _clone_from_template(
    tmpl_instances: dict,
    target_name: str | None,
    service_key: str,
    service_display_name: str,
) -> tuple[str, dict]:
    """
    Clone from the first template instance. Apply:
      - instance name normalization
      - process_name: '<ServiceDisplay> <CleanedDisplay>'
      - path rewrites: '/<service_key>/default' -> '/<service_key>/<cleaned_path>'
    Returns (instance_name_display, new_cfg)
    """
    if not tmpl_instances:
        raise HTTPException(500, detail="No template instances available.")

    base_name, base_cfg = next(iter(tmpl_instances.items()))
    new_cfg = copy.deepcopy(base_cfg)
    new_cfg["enabled"] = True
    requested = target_name if target_name else base_name
    cleaned_display, cleaned_path = normalize_instance_name(requested)
    inst_name = cleaned_display or "Instance"
    new_cfg["process_name"] = f"{service_display_name} {inst_name}"

    _rewrite_paths_for_instance(new_cfg, service_key, cleaned_path)

    return inst_name, new_cfg


def _template_default_port(template_cfg: dict, service_key: str) -> int | None:
    """
    Get the default port for a service from its template configuration.
    """
    svc = template_cfg.get(service_key)
    if not isinstance(svc, dict):
        return None
    if "instances" in svc and isinstance(svc["instances"], dict):
        first = next(iter(svc["instances"].values()), {})
        if isinstance(first, dict) and isinstance(first.get("port"), int):
            return first["port"]
    if isinstance(svc.get("port"), int):
        return svc["port"]
    return None


def _gather_used_ports(instances: dict, exclude_inst: str | None = None) -> set[int]:
    """
    Gather all ports used by the given instances, excluding `exclude_inst` if provided.
    """
    used = set()
    if not isinstance(instances, dict):
        return used
    for name, cfg in instances.items():
        if exclude_inst is not None and name == exclude_inst:
            continue
        if isinstance(cfg, dict):
            p = cfg.get("port")
            if isinstance(p, int):
                used.add(p)
    return used


def _find_free_port(start_port: int, used_ports: set[int], service_key: str) -> int:
    """
    Find a free port starting from `start_port`, avoiding `used_ports`.
    """
    low, high = None, None
    if service_key in SERVICE_PORT_RANGES:
        low, high = SERVICE_PORT_RANGES[service_key]
    port = max(start_port, low) if low is not None else start_port
    while True:
        if (
            port not in used_ports
            and _is_port_available(port)
            and (high is None or port <= high)
        ):
            return port
        if high is not None and port > high:
            high = None
        port += 1


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
    used_ports: dict[int, str],
    desired: int,
    service_key: str,
    owner: str,
    logger,
    label: str = "port",
) -> int | None:
    """
    Reserve a port globally, auto-shifting if it's already used by another owner.
    """
    if not isinstance(desired, int) or desired <= 0:
        return None

    existing_owner = used_ports.get(desired)
    if existing_owner and existing_owner != owner:
        new_port = _find_free_port(desired + 1, set(used_ports.keys()), service_key)
        logger.info(
            f"[{service_key}] Port {desired} already in use; assigning {new_port} for {label}."
        )
        used_ports[new_port] = owner
        return new_port
    if not _is_port_available(desired):
        new_port = _find_free_port(desired + 1, set(used_ports.keys()), service_key)
        logger.info(
            f"[{service_key}] Port {desired} already in use by another process; "
            f"assigning {new_port} for {label}."
        )
        used_ports[new_port] = owner
        return new_port

    used_ports[desired] = owner
    return desired


def _reserve_config_port(
    service_key: str,
    cfg: dict,
    field: str,
    used_ports: dict[int, str],
    logger,
    owner_suffix: str | None = None,
    label: str | None = None,
    default: int | None = None,
) -> None:
    """
    Reserve a global port for a config field, updating config if auto-shifted.
    """
    desired = cfg.get(field, default)
    if not isinstance(desired, int) or desired <= 0:
        if isinstance(default, int) and default > 0:
            desired = default
        else:
            return

    owner = f"{service_key}:{owner_suffix or field}"
    chosen = _reserve_port(
        used_ports=used_ports,
        desired=desired,
        service_key=service_key,
        owner=owner,
        logger=logger,
        label=label or field,
    )
    if chosen is None:
        return
    if cfg.get(field) != chosen:
        cfg[field] = chosen
        CONFIG_MANAGER.save_config()


def _seed_used_ports(config: dict, used_ports: dict[int, str], logger=None) -> None:
    """
    Seed port ownership from enabled configs to avoid global collisions.
    """
    if not isinstance(config, dict):
        return

    def _add(port: int | None, owner: str) -> None:
        if not isinstance(port, int) or port <= 0:
            return
        if port in used_ports and used_ports[port] != owner:
            if logger:
                logger.warning(
                    "Port %s already reserved by %s; %s may be auto-shifted.",
                    port,
                    used_ports[port],
                    owner,
                )
            return
        used_ports[port] = owner

    for key, cfg in config.items():
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


def _ensure_unique_instance_port(
    service_key: str,
    inst_name: str,
    inst_cfg: dict,
    instances: dict,
    template_config: dict,
    logger,
) -> None:
    """
    Ensure inst_cfg['port'] is unique among this core's instances.
    Excludes `inst_name` itself when calculating collisions.
    """
    used = _gather_used_ports(instances, exclude_inst=inst_name)
    desired = inst_cfg.get("port")
    if not isinstance(desired, int) or desired <= 0:
        tmpl_default = _template_default_port(template_config, service_key)
        desired = tmpl_default or DEFAULT_SERVICE_PORTS.get(service_key, 7000)

    if desired in used:
        new_port = _find_free_port(desired + 1, used, service_key)
        logger.info(
            f"[{service_key}] Port {desired} already in use; assigning {new_port} "
            f"for instance '{inst_name}'."
        )
        inst_cfg["port"] = new_port
        CONFIG_MANAGER.save_config()
    else:
        final_port = desired
        if not _is_port_available(desired):
            final_port = _find_free_port(desired + 1, used, service_key)
            logger.info(
                f"[{service_key}] Port {desired} already in use by another process; "
                f"assigning {final_port} for instance '{inst_name}'."
            )
        inst_cfg["port"] = final_port
        if final_port != desired:
            CONFIG_MANAGER.save_config()
        logger.debug(
            f"[{service_key}] Using port {final_port} for instance '{inst_name}'."
        )


def _start_optional_service(
    opt_key: str,
    opt_cfg: dict,
    merged_options: dict,
    used_ports: dict[int, str],
    updater,
    api_state,
    logger,
    template_config: dict,
) -> None:
    if "instances" in opt_cfg and isinstance(opt_cfg["instances"], dict):
        instances = opt_cfg["instances"]
        for inst_name, inst_cfg in instances.items():
            if not isinstance(inst_cfg, dict):
                continue
            if not inst_cfg.get("enabled"):
                inst_cfg["enabled"] = True
                CONFIG_MANAGER.save_config()

            apply_service_options(inst_cfg, merged_options, logger)
            _ensure_unique_instance_port(
                service_key=opt_key,
                inst_name=inst_name,
                inst_cfg=inst_cfg,
                instances=instances,
                template_config=template_config,
                logger=logger,
            )
            _reserve_config_port(
                opt_key,
                inst_cfg,
                "port",
                used_ports,
                logger,
                owner_suffix=inst_name,
                label=f"{inst_name} port",
            )

            proc = inst_cfg.get("process_name")
            if not proc:
                raise HTTPException(
                    500, detail=f"Process name not defined for '{opt_key}:{inst_name}'."
                )
            logger.info(f"Starting optional service: {proc}")
            if not wait_for_process_running(api_state, proc):
                updater.auto_update(
                    proc, enable_update=inst_cfg.get("auto_update", False)
                )
                wait_for_process_running(api_state, proc)
        return

    if not opt_cfg.get("enabled"):
        opt_cfg["enabled"] = True
        CONFIG_MANAGER.save_config()

    apply_service_options(opt_cfg, merged_options, logger)
    _reserve_config_port(opt_key, opt_cfg, "port", used_ports, logger)
    if opt_key == "nzbdav":
        _reserve_config_port(
            "nzbdav",
            opt_cfg,
            "frontend_port",
            used_ports,
            logger,
            label="frontend",
        )
        _reserve_config_port(
            "nzbdav",
            opt_cfg,
            "backend_port",
            used_ports,
            logger,
            label="backend",
        )

    proc = opt_cfg.get("process_name")
    logger.info(f"Starting optional service: {proc}")
    if not wait_for_process_running(api_state, proc):
        updater.auto_update(proc, enable_update=opt_cfg.get("auto_update", False))
        wait_for_process_running(api_state, proc)


@process_router.post("/start-core-service")
async def start_core_services(
    request: UnifiedStartRequest,
    updater=Depends(get_updater),
    api_state=Depends(get_api_state),
    logger=Depends(get_logger),
    current_user: str = Depends(get_optional_current_user),
):
    outcome = await run_in_threadpool(_run_startup, request, updater, api_state, logger)
    return outcome


def _run_startup(request: UnifiedStartRequest, updater, api_state, logger):
    priority_services = []
    remaining_services = []
    for svc in (
        [request.core_services]
        if isinstance(request.core_services, CoreServiceConfig)
        else request.core_services
    ):
        ident = normalize_identifier(svc.name)
        if ident in ("plex", "jellyfin", "emby"):
            priority_services.append(svc)
        else:
            remaining_services.append(svc)
    core_services = remaining_services
    raw_optionals = request.optional_services or []
    optional_services = [normalize_identifier(svc).lower() for svc in raw_optionals]
    optional_service_options = request.optional_service_options or {}

    results = []
    errors = []

    # Work in-place on the “single source of truth”
    config = CONFIG_MANAGER.config
    used_ports: dict[int, str] = {}
    _seed_used_ports(config, used_ports, logger)

    # Load template for creating new instances if needed
    with open("/utils/dumb_config.json") as f:
        template_config = json.load(f)

    #
    # 1) Start any “priority” core services (Plex/Jellyfin)
    #
    for service in priority_services:
        try:
            ident = normalize_identifier(service.name)
            cfg = config[ident]
            if not cfg.get("enabled"):
                cfg["enabled"] = True
                CONFIG_MANAGER.save_config()
            apply_service_options(
                cfg,
                service.service_options.get(ident, {}),
                logger,
            )
            _reserve_config_port(ident, cfg, "port", used_ports, logger)
            proc_name = cfg["process_name"]
            logger.info(f"Starting core service setup: {proc_name}")
            auto_up = cfg.get("auto_update", False)
            if not wait_for_process_running(api_state, proc_name):
                p, err = updater.auto_update(proc_name, enable_update=auto_up)
                if not p or not wait_for_process_running(api_state, proc_name):
                    raise HTTPException(
                        500, detail=f"{proc_name} failed to start. {err or ''}"
                    )
            results.append({"service": service.name, "status": "started"})
        except Exception as e:
            errors.append({"service": service.name, "error": str(e)})
    #
    # 2) Pre-start “must-have” services (e.g. Postgres)
    #
    if any(s in ["zilean", "pgadmin"] for s in optional_services):
        pg = config["postgres"]
        if not pg.get("enabled"):
            pg["enabled"] = True
            CONFIG_MANAGER.save_config()
        _reserve_config_port("postgres", pg, "port", used_ports, logger)
        pg_name = pg["process_name"]
        logger.info(f"Ensuring '{pg_name}' is running for optional service(s)...")
        if not wait_for_process_running(api_state, pg_name):
            updater.auto_update(pg_name, enable_update=False)
            wait_for_process_running(api_state, pg_name)

    #
    # 3) Start any “optional pre-core” services (those NOT in OPTIONAL_POST_CORE)
    #
    for opt in optional_services:
        if opt in config and opt not in OPTIONAL_POST_CORE:
            opt_cfg = config[opt]

            # Merge all service_options across services (priority + normal)
            merged_options = {}
            for svc in priority_services + core_services:
                if svc.service_options and opt in svc.service_options:
                    merged_options.update(svc.service_options[opt])
            if opt in optional_service_options:
                merged_options.update(optional_service_options[opt])
            _start_optional_service(
                opt_key=opt,
                opt_cfg=opt_cfg,
                merged_options=merged_options,
                used_ports=used_ports,
                updater=updater,
                api_state=api_state,
                logger=logger,
                template_config=template_config,
            )

    #
    # 3.1) Start any “optional post-core” services when no cores are selected
    #
    if not core_services:
        for opt in optional_services:
            if opt in config and opt in OPTIONAL_POST_CORE:
                opt_cfg = config[opt]
                merged_options = {}
                if opt in optional_service_options:
                    merged_options.update(optional_service_options[opt])
                _start_optional_service(
                    opt_key=opt,
                    opt_cfg=opt_cfg,
                    merged_options=merged_options,
                    used_ports=used_ports,
                    updater=updater,
                    api_state=api_state,
                    logger=logger,
                    template_config=template_config,
                )
    #
    # 4) Now start each core service in turn, handling dependencies as needed
    #
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
            debrid_services = (
                [core_service.debrid_service]
                if isinstance(core_service.debrid_service, str)
                else core_service.debrid_service or []
            )
            debrid_keys = (
                [core_service.debrid_key]
                if isinstance(core_service.debrid_key, str)
                else core_service.debrid_key or []
            )

            # Pad keys list if shorter
            if len(debrid_keys) < len(debrid_services):
                debrid_keys.extend([None] * (len(debrid_services) - len(debrid_keys)))

            logger.info(f"Starting core service setup: {process_name}")
            logger.debug(f"→ config_key='{config_key}', instance='{instance_name}'")

            # Validate core service
            if config_key not in CORE_SERVICE_DEPENDENCIES:
                raise HTTPException(400, detail=f"{process_name} is not a core service")
            dependencies = CORE_SERVICE_DEPENDENCIES[config_key]

            # ---- Determine effective options for this core service BEFORE deps ----
            # don't mutate config here; just compute the effective value by peeking
            # at the request's service_options and falling back to current config.
            effective_opts = {}

            so = core_service.service_options or {}

            # Accept options under the service key (e.g., "decypharr": {...})
            if isinstance(so.get(config_key), dict):
                effective_opts.update(so[config_key])

            # Also accept options under the instance name key if one was resolved
            # (applies when you start specific instances of core services)
            if instance_name and isinstance(so.get(instance_name), dict):
                # instance-specific options should override service-level ones
                effective_opts.update(so[instance_name])

            # Fall back to persisted config if the flag isn't in service_options
            use_embedded = bool(
                effective_opts.get(
                    "use_embedded_rclone",
                    (config.get(config_key, {}) or {}).get(
                        "use_embedded_rclone", False
                    ),
                )
            )

            # If decypharr uses embedded rclone, drop rclone from deps *now*
            if config_key == "decypharr" and use_embedded:
                logger.debug(
                    "Decypharr is using embedded rclone; removing 'rclone' from dependencies."
                )
                dependencies = [d for d in dependencies if d != "rclone"]
                # Ensure api_keys map exists in decypharr config
                api_keys_map = config[config_key].setdefault("api_keys", {})

                # Merge/update for each debrid service passed to _run_startup
                for svc_name, svc_key in zip(debrid_services, debrid_keys):
                    if svc_name and svc_key:
                        svc_name_lc = svc_name.strip().lower()
                        if (
                            not api_keys_map.get(svc_name_lc)
                            or api_keys_map[svc_name_lc] != svc_key
                        ):
                            api_keys_map[svc_name_lc] = svc_key
                            logger.debug(
                                f"Set Decypharr embedded rclone API key for {svc_name_lc}: {svc_key[:4]}..."
                            )
                            CONFIG_MANAGER.save_config()

            logger.debug(f"Dependencies for '{config_key}': {dependencies}")
            post_core_rclone = config_key == "nzbdav"
            post_core_rclone_processes = []

            #
            # 4.1) Handle zurg/rclone dependencies for this core service (multi-instance support)
            #
            num_instances = max(len(debrid_services), 1)
            for i in range(num_instances):
                for dep in dependencies:
                    if dep in ("zurg", "rclone"):
                        service_type = (
                            debrid_services[i]
                            if i < len(debrid_services)
                            else "realdebrid"
                        ).lower()
                        service_key = debrid_keys[i] if i < len(debrid_keys) else None

                        display = CORE_SERVICE_NAMES.get(
                            config_key, config_key.replace("_", " ").title()
                        )
                        if config_key == "decypharr":
                            display += f" ({service_type.title()})"
                        clean_display = (
                            display.lower()
                            .replace("(", "")
                            .replace(")", "")
                            .replace(" ", "_")
                        )
                        instance_key = display.strip()

                        instances = config[dep]["instances"]

                        # Check if instance already exists
                        inst = instance_key if instance_key in instances else None

                        if inst:
                            inst_cfg = instances[inst]
                            if not inst_cfg.get("enabled"):
                                inst_cfg["enabled"] = True
                                CONFIG_MANAGER.save_config()
                            apply_service_options(
                                inst_cfg,
                                core_service.service_options.get(inst, {}),
                                logger,
                            )
                            if (
                                dep == "zurg"
                                and service_type == "realdebrid"
                                and service_key
                            ):
                                inst_cfg["api_key"] = service_key
                                CONFIG_MANAGER.save_config()
                        else:
                            # no instance exists → clone from template, enable and override
                            base = template_config[dep]["instances"]["RealDebrid"]
                            new_cfg = copy.deepcopy(base)

                            # clean up any disabled “RealDebrid” placeholder
                            if "RealDebrid" in instances and not instances[
                                "RealDebrid"
                            ].get("enabled"):
                                del instances["RealDebrid"]
                                CONFIG_MANAGER.save_config()

                            if dep == "zurg":
                                # allocate a free port
                                local_used_ports = {
                                    c["port"] for c in instances.values() if "port" in c
                                }
                                port = 9090
                                while port in local_used_ports or port in used_ports:
                                    port += 1
                                new_cfg.update(
                                    {
                                        "enabled": True,
                                        "core_service": config_key,
                                        "process_name": f"Zurg w/ {display}",
                                        "config_dir": f"/zurg/RD/{display}",
                                        "config_file": f"/zurg/RD/{display}/config.yml",
                                        "command": f"/zurg/RD/{display}/zurg",
                                    }
                                )
                                reserved = _reserve_port(
                                    used_ports=used_ports,
                                    desired=port,
                                    service_key="zurg",
                                    owner=f"zurg:{instance_key}",
                                    logger=logger,
                                    label="port",
                                )
                                if reserved is not None:
                                    new_cfg["port"] = reserved
                                if service_type == "realdebrid" and service_key:
                                    new_cfg["api_key"] = service_key

                            else:  # rclone

                                rclone_cfg = {
                                    "enabled": True,
                                    "core_service": config_key,
                                    "process_name": f"Rclone w/ {display}",
                                    "key_type": (
                                        "nzbdav"
                                        if config_key == "nzbdav"
                                        else service_type
                                    ),
                                    "mount_name": clean_display,
                                    "log_file": f"/log/rclone_w_{clean_display}.log",
                                }
                                if config_key == "nzbdav":
                                    rclone_cfg.update(
                                        {
                                            "zurg_enabled": False,
                                            "decypharr_enabled": False,
                                            "zurg_config_file": "",
                                        }
                                    )
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
                                            "api_key": service_key or "",
                                        }
                                    )
                                new_cfg.update(rclone_cfg)

                            instances[instance_key] = new_cfg
                            CONFIG_MANAGER.save_config()
                            apply_service_options(
                                instances[instance_key],
                                core_service.service_options.get(dep, {}),
                                logger,
                            )

                        # start/update this instance
                        proc = config[dep]["instances"][instance_key]["process_name"]
                        ok = config[dep]["instances"][instance_key].get(
                            "auto_update", False
                        )
                        if post_core_rclone and dep == "rclone":
                            post_core_rclone_processes.append((proc, ok))
                        else:
                            proc_obj, err = updater.auto_update(proc, enable_update=ok)
                            if proc_obj and wait_for_process_running(api_state, proc):
                                logger.info(f"{proc} is running.")
                            else:
                                raise HTTPException(
                                    500,
                                    detail=f"{proc} failed to start. {err or ''}",
                                )
                    else:
                        # start any other dependencies that are not zurg/rclone
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
                                dep_proc,
                                enable_update=dep_cfg.get("auto_update", False),
                            )
                            if not wait_for_process_running(api_state, dep_proc):
                                raise HTTPException(
                                    500,
                                    detail=f"{dep_proc} failed to start. Please check the logs.",
                                )
                logger.debug(f"All dependencies for '{config_key}' are running.")
            #
            # 4.2) Finally, start the core service itself
            #
            core_cfg = config[config_key]
            is_instance_core = isinstance(core_cfg, dict) and "instances" in core_cfg
            if not is_instance_core:
                if not core_cfg.get("enabled"):
                    core_cfg["enabled"] = True
                    CONFIG_MANAGER.save_config()

            if isinstance(core_cfg, dict) and "instances" in core_cfg:
                instances = core_cfg.setdefault("instances", {})

                # Prefer explicit request value
                requested_inst = (
                    getattr(core_service, "instance_name", None) or instance_name
                )
                service_display = CORE_SERVICE_NAMES.get(
                    config_key, config_key.replace("_", " ").title()
                ).strip()

                tmpl_instances = template_config.get(config_key, {}).get(
                    "instances", {}
                )

                logger.debug(
                    f"[{config_key}] requested_inst resolved to: {requested_inst!r}"
                )

                targets = []

                if requested_inst:
                    # Normalize and always purge template placeholders (even if enabled)
                    display_name, _ = normalize_instance_name(requested_inst)
                    _purge_template_placeholders(instances, tmpl_instances, keep=None)

                    if display_name in instances:
                        targets = [display_name]
                    else:
                        if not tmpl_instances:
                            raise HTTPException(
                                500,
                                detail=f"No template instances defined for core service '{config_key}'.",
                            )
                        inst_name, new_cfg = _clone_from_template(
                            tmpl_instances,
                            display_name,
                            service_key=config_key,
                            service_display_name=service_display,
                        )
                        if inst_name in instances:
                            n = 2
                            base = inst_name
                            while f"{base} {n}" in instances:
                                n += 1
                            inst_name = f"{base} {n}"
                            new_cfg["process_name"] = f"{service_display} {inst_name}"

                        instances[inst_name] = new_cfg
                        CONFIG_MANAGER.save_config()
                        targets = [inst_name]
                else:
                    # No explicit target:
                    # 1) Ignore template placeholders when auto-selecting
                    template_names = set(tmpl_instances.keys())
                    enabled_non_templates = [
                        k
                        for k, v in instances.items()
                        if isinstance(v, dict)
                        and v.get("enabled")
                        and k not in template_names
                    ]

                    if enabled_non_templates:
                        targets = enabled_non_templates
                    else:
                        # If only templates exist and are disabled → create one real instance
                        only_templates_exist = instances and all(
                            k in template_names for k in instances.keys()
                        )
                        if not instances or only_templates_exist:
                            if not tmpl_instances:
                                raise HTTPException(
                                    500,
                                    detail=f"No template instances defined for core service '{config_key}'.",
                                )
                            inst_name, new_cfg = _clone_from_template(
                                tmpl_instances,
                                None,
                                service_key=config_key,
                                service_display_name=service_display,
                            )
                            if inst_name in instances:
                                n = 2
                                base = inst_name
                                while f"{base} {n}" in instances:
                                    n += 1
                                final_name = f"{base} {n}"
                                new_cfg["process_name"] = (
                                    f"{service_display} {final_name}"
                                )
                                inst_name = final_name
                            # Wipe templates to avoid confusion
                            _purge_template_placeholders(
                                instances, tmpl_instances, keep=None
                            )
                            instances[inst_name] = new_cfg
                            CONFIG_MANAGER.save_config()
                            targets = [inst_name]
                        else:
                            # There are instances but none enabled (or all are templates) — do nothing silently.
                            logger.info(
                                f"[{config_key}] No explicit instance requested and no non-template enabled instances; skipping."
                            )
                            targets = []

                # Start targets
                for inst_name in targets:
                    inst_cfg = instances.get(inst_name)
                    if not isinstance(inst_cfg, dict):
                        raise HTTPException(
                            500,
                            detail=f"Invalid instance config for '{config_key}:{inst_name}'",
                        )

                    if not inst_cfg.get("enabled"):
                        inst_cfg["enabled"] = True
                        CONFIG_MANAGER.save_config()

                    inst_opts = {}
                    if core_service.service_options:
                        if inst_name in core_service.service_options:
                            inst_opts.update(
                                core_service.service_options.get(inst_name, {})
                            )
                        if config_key in core_service.service_options:
                            for k, v in core_service.service_options[
                                config_key
                            ].items():
                                inst_opts.setdefault(k, v)

                    apply_service_options(inst_cfg, inst_opts, logger)

                    _ensure_unique_instance_port(
                        service_key=config_key,
                        inst_name=inst_name,
                        inst_cfg=inst_cfg,
                        instances=instances,
                        template_config=template_config,
                        logger=logger,
                    )
                    _reserve_config_port(
                        config_key,
                        inst_cfg,
                        "port",
                        used_ports,
                        logger,
                        owner_suffix=inst_name,
                        label=f"{inst_name} port",
                    )

                    proc_name = inst_cfg.get("process_name")
                    if not proc_name:
                        raise HTTPException(
                            500,
                            detail=f"Process name not defined for '{config_key}:{inst_name}'",
                        )

                    auto_up = inst_cfg.get("auto_update", False)
                    if not wait_for_process_running(api_state, proc_name):
                        p, err = updater.auto_update(proc_name, enable_update=auto_up)
                        if not p or not wait_for_process_running(api_state, proc_name):
                            raise HTTPException(
                                500, detail=f"{proc_name} failed to start. {err or ''}"
                            )

            else:
                # singleton case
                apply_service_options(
                    core_cfg, core_service.service_options.get(config_key, {}), logger
                )
                _reserve_config_port(config_key, core_cfg, "port", used_ports, logger)
                if config_key == "nzbdav":
                    _reserve_config_port(
                        "nzbdav",
                        core_cfg,
                        "frontend_port",
                        used_ports,
                        logger,
                        label="frontend",
                    )
                    _reserve_config_port(
                        "nzbdav",
                        core_cfg,
                        "backend_port",
                        used_ports,
                        logger,
                        label="backend",
                    )
                proc_name = core_cfg["process_name"]
                auto_up = core_cfg.get("auto_update", False)
                if not wait_for_process_running(api_state, proc_name):
                    p, err = updater.auto_update(proc_name, enable_update=auto_up)
                    if not p or not wait_for_process_running(api_state, proc_name):
                        raise HTTPException(
                            500, detail=f"{proc_name} failed to start. {err or ''}"
                        )

            if post_core_rclone_processes:
                for proc_name, ok in post_core_rclone_processes:
                    if wait_for_process_running(api_state, proc_name):
                        continue
                    p, err = updater.auto_update(proc_name, enable_update=ok)
                    if not p or not wait_for_process_running(api_state, proc_name):
                        raise HTTPException(
                            500,
                            detail=f"{proc_name} failed to start. {err or ''}",
                        )
            #
            # 4.3) Start any “optional post-core” services
            #
            for opt in optional_services:
                if opt in config and opt in OPTIONAL_POST_CORE:
                    oc = config[opt]
                    merged_options = {}
                    if (
                        core_service.service_options
                        and opt in core_service.service_options
                    ):
                        merged_options.update(core_service.service_options[opt])
                    if opt in optional_service_options:
                        merged_options.update(optional_service_options[opt])
                    _start_optional_service(
                        opt_key=opt,
                        opt_cfg=oc,
                        merged_options=merged_options,
                        used_ports=used_ports,
                        updater=updater,
                        api_state=api_state,
                        logger=logger,
                        template_config=template_config,
                    )

            results.append({"service": core_service.name, "status": "started"})

        except HTTPException as e:
            errors.append({"service": core_service.name, "error": e.detail})

    try:
        from utils.huntarr_settings import any_arr_uses_huntarr, patch_huntarr_config

        if any_arr_uses_huntarr():
            huntarr_cfg = config.get("huntarr", {})
            if isinstance(huntarr_cfg, dict):
                instances = huntarr_cfg.get("instances", {}) or {}
                enabled_any = any(
                    isinstance(inst, dict) and inst.get("enabled")
                    for inst in instances.values()
                )
                if instances and not enabled_any:
                    first = next(iter(instances.values()))
                    if isinstance(first, dict):
                        first["enabled"] = True
                        CONFIG_MANAGER.save_config()

                merged_options = optional_service_options.get("huntarr", {})
                _start_optional_service(
                    opt_key="huntarr",
                    opt_cfg=huntarr_cfg,
                    merged_options=merged_options,
                    used_ports=used_ports,
                    updater=updater,
                    api_state=api_state,
                    logger=logger,
                    template_config=template_config,
                )

                ok, err = patch_huntarr_config()
                if not ok and err:
                    logger.warning("Huntarr config sync failed: %s", err)
    except Exception as exc:
        logger.warning("Huntarr auto-config skipped: %s", exc)

    # Final persist & reload to ensure in-memory matches on-disk
    CONFIG_MANAGER.save_config()
    CONFIG_MANAGER.reload()

    logger.info("Core services started successfully.")
    return {"results": results, "errors": errors}


@process_router.get("/core-services")
async def get_core_services(
    logger=Depends(get_logger), current_user: str = Depends(get_optional_current_user)
):
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
        instance_options: Dict[str, Dict[str, Any]] = {}

        core_block = default_conf.get(key, {}) or {}
        inst_tmpls = (
            core_block.get("instances") if isinstance(core_block, dict) else None
        )
        supports_instances = isinstance(inst_tmpls, dict) and len(inst_tmpls) > 0

        if supports_instances:
            # Use the first instance as the representative defaults for the core service block
            first_inst_name, first_inst_cfg = next(iter(inst_tmpls.items()))
            svc_opts[key] = {
                k: v for k, v in first_inst_cfg.items() if k in BASIC_FIELDS
            }
            # Also surface per-instance defaults so UI can present a selector
            for iname, icfg in inst_tmpls.items():
                instance_options[iname] = {
                    k: v for k, v in icfg.items() if k in BASIC_FIELDS
                }
        else:
            # Singleton template
            svc_opts[key] = {
                k: v
                for k, v in core_block.items()
                if isinstance(core_block, dict) and k in BASIC_FIELDS
            }

        # Dependencies (keep your current behavior)
        for dep in CORE_SERVICE_DEPENDENCIES.get(key, []):
            if dep in ("zurg", "rclone"):
                instances = default_conf.get(dep, {}).get("instances", {})
                inst_cfg = next(
                    (cfg for cfg in instances.values() if has_core_service(cfg, key)),
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

        # Field descriptions
        svc_opt_desc: Dict[str, Dict[str, str]] = {}
        for svc_key, opts in svc_opts.items():
            svc_opt_desc[svc_key] = {
                field: SERVICE_OPTION_DESCRIPTIONS[field] for field in opts.keys()
            }
        # Instance option descriptions mirror BASIC_FIELDS
        instance_opt_desc: Dict[str, Dict[str, str]] = {}
        if supports_instances:
            for iname, opts in instance_options.items():
                instance_opt_desc[iname] = {
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
                "supports_instances": supports_instances,
                "instance_options": instance_options,  # only populated if supports_instances
                "instance_option_descriptions": instance_opt_desc,  # only populated if supports_instances
            }
        )

    return {"core_services": core_services}


@process_router.get("/optional-services")
async def get_optional_services(
    logger=Depends(get_logger),
    current_user: str = Depends(get_optional_current_user),
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
        svc_opts = {}
        instance_options: Dict[str, Dict[str, Any]] = {}
        supports_instances = False
        if isinstance(raw, dict) and isinstance(raw.get("instances"), dict):
            tmpl_instances = raw.get("instances") or {}
            supports_instances = bool(tmpl_instances)
            if supports_instances:
                first_inst = next(iter(tmpl_instances.values()), {})
                if isinstance(first_inst, dict):
                    svc_opts = {
                        k: v for k, v in first_inst.items() if k in BASIC_FIELDS
                    }
                for iname, icfg in tmpl_instances.items():
                    if isinstance(icfg, dict):
                        instance_options[iname] = {
                            k: v for k, v in icfg.items() if k in BASIC_FIELDS
                        }
        if not svc_opts and isinstance(raw, dict):
            svc_opts = {
                k: raw[k]
                for k in SERVICE_OPTION_DESCRIPTIONS
                if k in raw and k in BASIC_FIELDS
            }

        svc_opt_desc = {
            field: SERVICE_OPTION_DESCRIPTIONS[field] for field in svc_opts.keys()
        }
        instance_opt_desc: Dict[str, Dict[str, str]] = {}
        if supports_instances:
            for iname, opts in instance_options.items():
                instance_opt_desc[iname] = {
                    field: SERVICE_OPTION_DESCRIPTIONS[field] for field in opts.keys()
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
                "supports_instances": supports_instances,
                "instance_options": instance_options,
                "instance_option_descriptions": instance_opt_desc,
            }
        )

    return {"optional_services": results}


@process_router.get("/capabilities")
async def get_capabilities(current_user: str = Depends(get_optional_current_user)):
    return {
        "optional_only_onboarding": True,
        "optional_service_options": True,
    }
