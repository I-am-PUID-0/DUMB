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
from utils.dependency_map import build_conditional_dependency_map, filter_conditional_deps_for_instance
from utils.versions import Versions
import json, copy, time, glob, re, socket, errno, psutil, os, threading


class ServiceRequest(BaseModel):
    process_name: str


class UpdateCheckRequest(BaseModel):
    process_name: str
    force: Optional[bool] = False


class UpdateInstallRequest(BaseModel):
    process_name: str
    allow_override: Optional[bool] = False
    target: Optional[str] = None


class RescheduleAutoUpdateRequest(BaseModel):
    process_name: str


class RescheduleSymlinkBackupRequest(BaseModel):
    process_name: str


class SymlinkRewriteRule(BaseModel):
    from_prefix: str
    to_prefix: str


class SymlinkRootMigration(BaseModel):
    from_root: str
    to_root: str


class SymlinkRepairRequest(BaseModel):
    process_name: Optional[str] = None
    roots: Optional[List[str]] = None
    rewrite_rules: Optional[List[SymlinkRewriteRule]] = None
    root_migrations: Optional[List[SymlinkRootMigration]] = None
    presets: Optional[List[str]] = None
    dry_run: Optional[bool] = True
    include_broken: Optional[bool] = True
    backup_path: Optional[str] = None
    overwrite_existing: Optional[bool] = False
    copy_instead_of_move: Optional[bool] = False


class SymlinkManifestBackupRequest(BaseModel):
    process_name: Optional[str] = None
    roots: Optional[List[str]] = None
    backup_path: str
    include_broken: Optional[bool] = True


class SymlinkManifestRestoreRequest(BaseModel):
    process_name: Optional[str] = None
    manifest_path: str
    dry_run: Optional[bool] = True
    overwrite_existing: Optional[bool] = False
    restore_broken: Optional[bool] = True


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
DEPENDENCY_INSTANCE_SCOPED_KEYS = {"rclone", "zurg"}
DEPENDENCY_TRUTH_TABLE = [
    {
        "signal": "core_service_map",
        "classification": "hard",
        "strength": "hard_runtime",
        "description": "Static core-service dependency map used by backend startup ordering.",
    },
    {
        "signal": "core_service_fields",
        "classification": "hard",
        "strength": "hard_configured",
        "description": "Instance-level linkage from core_service/core_services fields.",
    },
    {
        "signal": "wait_for_url",
        "classification": "hard",
        "strength": "hard_runtime",
        "description": "Process start blocks until required URLs respond.",
    },
    {
        "signal": "wait_for_dir",
        "classification": "hard",
        "strength": "hard_runtime",
        "description": "Process start blocks until required directory path exists.",
    },
    {
        "signal": "wait_for_mounts",
        "classification": "hard",
        "strength": "hard_runtime",
        "description": "Process start blocks until required mount paths are mounted.",
    },
    {
        "signal": "rclone_provider_zurg",
        "classification": "hard",
        "strength": "hard_runtime",
        "description": "Rclone instance is configured to use a Zurg WebDAV provider.",
    },
    {
        "signal": "rclone_provider_decypharr",
        "classification": "hard",
        "strength": "hard_runtime",
        "description": "Rclone instance is configured to use Decypharr as provider.",
    },
    {
        "signal": "rclone_provider_nzbdav",
        "classification": "hard",
        "strength": "hard_runtime",
        "description": "Rclone instance is configured to use NzbDAV as provider.",
    },
    {
        "signal": "zilean_optional_integration",
        "classification": "linkage",
        "strength": "soft_linkage",
        "description": "Service can integrate with Zilean based on config/workflow, but startup is not hard-blocked.",
    },
    {
        "signal": "non_core_dependency_map",
        "classification": "hard",
        "strength": "hard_runtime",
        "description": "Static dependency linkage for non-core services that still require ordered startup.",
    },
    {
        "signal": "conditional_startup_map",
        "classification": "hard",
        "strength": "hard_runtime",
        "description": "Config-conditional startup dependency (e.g., tautulli\u2192plex when plex is enabled, prowlarr\u2192sonarr when sonarr is enabled).",
    },
    {
        "signal": "documented_integration",
        "classification": "linkage",
        "strength": "soft_linkage",
        "description": "Documented service integration (e.g., Seerr routes requests to Sonarr/Radarr) that is not startup-blocking.",
    },
]
DEPENDENCY_SIGNAL_STRENGTH = {
    "core_service_map": "hard_runtime",
    "core_service_fields": "hard_configured",
    "wait_for_url": "hard_runtime",
    "wait_for_dir": "hard_runtime",
    "wait_for_mounts": "hard_runtime",
    "rclone_provider_zurg": "hard_runtime",
    "rclone_provider_decypharr": "hard_runtime",
    "rclone_provider_nzbdav": "hard_runtime",
    "zilean_optional_integration": "soft_linkage",
    "non_core_dependency_map": "hard_runtime",
    "conditional_startup_map": "hard_runtime",
    "documented_integration": "soft_linkage",
}
ZILEAN_OPTIONAL_LINK_KEYS = {
    "sonarr",
    "radarr",
    "lidarr",
    "whisparr",
    "prowlarr",
    "riven_backend",
    "cli_debrid",
}
NON_CORE_HARD_DEPENDENCIES = {
    "riven_frontend": ["riven_backend"],
    "zilean": ["postgres"],
    "pgadmin": ["postgres"],
}
DOCUMENTED_INTEGRATION_LINKS = {
    "seerr": ["sonarr", "radarr"],
}

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
    "profilarr": "https://github.com/Dictionarry-Hub/profilarr",
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
    "profilarr": "https://github.com/sponsors/Dictionarry-Hub",
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
    "profilarr": 6868,
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
    "profilarr": [],
}

# Temporarily hide not-ready services from onboarding core selection.
ONBOARDING_HIDDEN_CORE_SERVICES = {"bazarr"}

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
    "profilarr": "Profilarr",
    "bazarr": "Bazarr",
    "tautulli": "Tautulli",
    "pgadmin": "pgAdmin",
    "riven_frontend": "Riven Frontend",
    "zilean": "Zilean",
    "postgres": "PostgreSQL",
    "rclone": "rclone",
    "zurg": "Zurg",
    "traefik": "Traefik",
    "dumb_api_service": "DUMB API",
    "dumb_frontend": "DUMB Frontend",
    "cli_battery": "CLI Battery",
    "phalanx_db": "Phalanx DB",
    "plex_debrid": "Plex Debrid",
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
    "profilarr": """\
Profilarr
- Profile and custom format management for Sonarr and Radarr.
- Syncs and version-controls quality profiles, custom formats, and media management settings.
- Useful for keeping multiple Arr stacks aligned across libraries or households.

Documentation: https://dumbarr.com/services/core/profilarr""",
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
    "auto_update_start_time": "24-hour start time for the auto-update schedule (HH:MM).",
    "symlink_backup_enabled": "Enable scheduled standalone symlink snapshot backups for this service.",
    "symlink_backup_interval": "Hours between scheduled symlink snapshot backups.",
    "symlink_backup_start_time": "24-hour start time for the symlink-backup schedule (HH:MM).",
    "symlink_backup_path": "Backup manifest destination template. Supports {timestamp}, {date}, {time}, {process_name}, {process_slug}.",
    "symlink_backup_include_broken": "Include symlink entries whose targets currently do not exist in scheduled backups.",
    "symlink_backup_roots": "Optional roots list (array or comma/newline text) to scope scheduled symlink backups.",
    "symlink_backup_retention_count": "Number of scheduled backup manifests to retain per service template (0 disables pruning).",
    "plex_claim": "Token used to claim the Plex Media Server. https://www.plex.tv/claim",
    "friendly_name": "A user-friendly name for the Plex Media Server.",
    "setup_email": "Email address pgAdmin4 login.",
    "setup_password": "Password for pgAdmin4 login.",
    "origin": "CORS origin for the service",
    "mount_type": "Decypharr mount type: dfs, rclone, external_rclone, or none.",
    "mount_path": "Decypharr mount path for DFS or rclone mounts.",
    "use_huntarr": "If true, auto-configures Huntarr for this Arr instance.",
    "use_profilarr": "If true, auto-configures Profilarr for this Arr instance.",
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
    api_state=Depends(get_api_state),
    updater=Depends(get_updater),
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
        update_status = api_state.get_update_status(process_name) if api_state else None
        symlink_backup_status = (
            api_state.get_symlink_backup_status(process_name) if api_state else None
        )
        supports_manual_update = False
        if updater:
            supports_manual_update = updater.supports_manual_update(config_key, config)

        return {
            "process_name": process_name,
            "config": config,
            "version": version,
            "config_key": config_key,
            "update_status": update_status,
            "symlink_backup_status": symlink_backup_status,
            "supports_manual_update": supports_manual_update,
        }
    except Exception as e:
        logger.error(f"Failed to load process: {e}")
        raise HTTPException(status_code=500, detail="Failed to load process")


@process_router.get("/processes")
def fetch_processes(
    logger=Depends(get_logger), current_user: str = Depends(get_optional_current_user)
):
    try:
        return {"processes": _collect_process_entries()}
    except Exception as e:
        logger.error(f"Failed to load processes: {e}")
        raise HTTPException(status_code=500, detail="Failed to load processes")


@process_router.get("/dependency-graph")
def dependency_graph(
    process_name: str = Query(..., description="The process name to analyze"),
    scope: str = Query(
        "runtime",
        description="Graph detail scope: runtime (hard runtime + hard configured) or all (includes soft linkage).",
    ),
    api_state=Depends(get_api_state),
    logger=Depends(get_logger),
    current_user: str = Depends(get_optional_current_user),
):
    try:
        target_name = str(process_name or "").strip()
        if not target_name:
            raise HTTPException(status_code=400, detail="process_name is required")
        scope_mode = str(scope or "runtime").strip().lower()
        if scope_mode not in {"runtime", "all"}:
            raise HTTPException(
                status_code=400, detail="scope must be 'runtime' or 'all'"
            )

        processes = _collect_process_entries()
        conditional_deps = build_conditional_dependency_map(
            lambda key: CONFIG_MANAGER.config.get(key, {})
        )
        by_process_name: dict[str, dict] = {}
        by_config_key: dict[str, list[dict]] = {}
        port_to_entries: dict[int, list[dict]] = {}
        mount_to_entries: dict[str, list[dict]] = {}
        status_by_process: dict[str, str] = {}

        for entry in processes:
            proc_name = str(entry.get("process_name") or "").strip()
            if not proc_name:
                continue
            normalized_proc = _normalize_dep_token(proc_name)
            if normalized_proc:
                by_process_name[normalized_proc] = entry

            config_key = _normalize_dep_token(entry.get("config_key") or "")
            if config_key:
                by_config_key.setdefault(config_key, []).append(entry)

            status_by_process[proc_name] = (
                str(api_state.get_status(proc_name) if api_state else "unknown").lower()
            )
            for port in _dep_process_ports(entry):
                port_to_entries.setdefault(port, []).append(entry)
            for mount_path in _dep_process_mount_points(entry):
                mount_to_entries.setdefault(mount_path, []).append(entry)

        target = by_process_name.get(_normalize_dep_token(target_name))
        if not target:
            raise HTTPException(status_code=404, detail="Process not found")

        target_proc_name = str(target.get("process_name") or "")
        target_key = _normalize_dep_token(target.get("config_key") or "")
        target_config = target.get("config") if isinstance(target.get("config"), dict) else {}
        instance_conditional_deps = filter_conditional_deps_for_instance(
            conditional_deps, target_key, target_config
        )
        target_core_refs = _dep_extract_refs_from_config(target_config)
        target_primary_core = target_core_refs[0] if target_core_refs else ""
        core_entry = {
            "key": target_key,
            "name": CORE_SERVICE_NAMES.get(target_key) or target.get("name") or target_proc_name,
            "dependencies": CORE_SERVICE_DEPENDENCIES.get(target_key, []),
        }

        def resolve_ref_entries(refs: list[str], source_core_refs: list[str]) -> list[dict]:
            resolved: list[dict] = []
            seen: set[str] = set()
            core_ref_set = {
                _normalize_dep_token(entry) for entry in (source_core_refs or []) if entry
            }

            def add_entry(entry: dict):
                proc_name = str(entry.get("process_name") or "")
                if not proc_name or proc_name in seen:
                    return
                seen.add(proc_name)
                resolved.append(entry)

            for ref in refs:
                normalized_ref = _normalize_dep_token(ref)
                if not normalized_ref:
                    continue
                if normalized_ref in by_process_name:
                    add_entry(by_process_name[normalized_ref])
                    continue
                group = by_config_key.get(normalized_ref, [])
                if normalized_ref in DEPENDENCY_INSTANCE_SCOPED_KEYS and core_ref_set:
                    group = [
                        entry
                        for entry in group
                        if any(
                            _normalize_dep_token(core_ref) in core_ref_set
                            for core_ref in _dep_extract_refs_from_config(entry.get("config") or {})
                        )
                    ]
                for entry in group:
                    add_entry(entry)

            return resolved

        def resolve_rclone_provider_entries(
            process_entry: dict,
            process_config: dict,
            primary_core_key: str = "",
        ) -> list[tuple[dict, str]]:
            if _normalize_dep_token(process_entry.get("config_key") or "") != "rclone":
                return []

            results: list[tuple[dict, str]] = []
            seen: set[tuple[str, str]] = set()

            def add_by_key(dep_key: str, reason: str, scoped: bool = False):
                normalized_key = _normalize_dep_token(dep_key)
                if not normalized_key:
                    return
                entries = list(by_config_key.get(normalized_key, []))
                if scoped and primary_core_key:
                    entries = [
                        entry
                        for entry in entries
                        if has_core_service(entry.get("config") or {}, primary_core_key)
                    ]
                for entry in entries:
                    proc_name = str(entry.get("process_name") or "")
                    marker = (proc_name, reason)
                    if not proc_name or marker in seen:
                        continue
                    seen.add(marker)
                    results.append((entry, reason))

            if bool(process_config.get("zurg_enabled")):
                add_by_key("zurg", "rclone_provider_zurg", scoped=True)
            if bool(process_config.get("decypharr_enabled")):
                add_by_key("decypharr", "rclone_provider_decypharr")
            if _normalize_dep_token(process_config.get("key_type") or "") == "nzbdav":
                add_by_key("nzbdav", "rclone_provider_nzbdav")

            return results

        source_core_refs = sorted(
            set(_dep_extract_refs_from_config(target_config) + [target_key])
        )

        outgoing_core_entries = resolve_ref_entries(source_core_refs, source_core_refs)
        outgoing_wait_url_entries = resolve_ref_entries(
            _dep_extract_wait_refs(target_config.get("wait_for_url")), source_core_refs
        )
        outgoing_wait_dir_entries = resolve_ref_entries(
            _dep_extract_wait_refs(target_config.get("wait_for_dir")), source_core_refs
        )
        outgoing_reasons: dict[str, set[str]] = {}
        incoming_reasons: dict[str, set[str]] = {}
        outgoing_map: dict[str, dict] = {}

        def add_outgoing(entry: dict, reason: str):
            proc_name = str(entry.get("process_name") or "")
            if not proc_name or proc_name == target_proc_name:
                return
            outgoing_reasons.setdefault(proc_name, set()).add(reason)
            outgoing_map[proc_name] = entry

        def add_incoming(entry: dict, reason: str):
            proc_name = str(entry.get("process_name") or "")
            if not proc_name or proc_name == target_proc_name:
                return
            incoming_reasons.setdefault(proc_name, set()).add(reason)
            incoming_map[proc_name] = entry

        for entry in outgoing_core_entries:
            add_outgoing(entry, "core_service_fields")
        for entry in outgoing_wait_url_entries:
            add_outgoing(entry, "wait_for_url")
        for entry in outgoing_wait_dir_entries:
            add_outgoing(entry, "wait_for_dir")
        for port in _dep_extract_wait_ports(target_config.get("wait_for_url")):
            for candidate in port_to_entries.get(port, []):
                add_outgoing(candidate, "wait_for_url")

        for raw_mount in target_config.get("wait_for_mounts") or []:
            wait_mount = _dep_norm_path(raw_mount)
            if not wait_mount:
                continue
            for mount_path, candidates in mount_to_entries.items():
                if not mount_path:
                    continue
                if wait_mount == mount_path or wait_mount.startswith(mount_path + "/"):
                    for candidate in candidates:
                        add_outgoing(candidate, "wait_for_mounts")
                elif mount_path.startswith(wait_mount + "/"):
                    for candidate in candidates:
                        add_outgoing(candidate, "wait_for_mounts")
        for wait_dir in _dep_extract_wait_dirs(target_config.get("wait_for_dir")):
            for mount_path, candidates in mount_to_entries.items():
                if not mount_path:
                    continue
                if wait_dir == mount_path or wait_dir.startswith(mount_path + "/"):
                    for candidate in candidates:
                        add_outgoing(candidate, "wait_for_dir")
                elif mount_path.startswith(wait_dir + "/"):
                    for candidate in candidates:
                        add_outgoing(candidate, "wait_for_dir")
        for provider_entry, provider_reason in resolve_rclone_provider_entries(
            target, target_config, target_primary_core
        ):
            add_outgoing(provider_entry, provider_reason)
        for dep_key in NON_CORE_HARD_DEPENDENCIES.get(target_key, []):
            for dep_entry in by_config_key.get(_normalize_dep_token(dep_key), []):
                add_outgoing(dep_entry, "non_core_dependency_map")
        for dep_key in instance_conditional_deps.get(target_key, set()):
            dep_norm = _normalize_dep_token(dep_key)
            for dep_entry in by_config_key.get(dep_norm, []):
                # For instance-scoped deps, only annotate entries already
                # detected by specific signals — conditional_startup_map is
                # service-level and cannot distinguish individual instances.
                if dep_norm in DEPENDENCY_INSTANCE_SCOPED_KEYS:
                    entry_name = str(dep_entry.get("process_name") or "")
                    if entry_name not in outgoing_map:
                        continue
                add_outgoing(dep_entry, "conditional_startup_map")

        target_ports = set(_dep_process_ports(target))
        target_mounts = set(_dep_process_mount_points(target))
        incoming_map: dict[str, dict] = {}
        for candidate in processes:
            candidate_name = str(candidate.get("process_name") or "")
            if not candidate_name or candidate_name == target_proc_name:
                continue
            candidate_cfg = (
                candidate.get("config")
                if isinstance(candidate.get("config"), dict)
                else {}
            )
            candidate_core_refs = sorted(
                set(
                    _dep_extract_refs_from_config(candidate_cfg)
                    + [_normalize_dep_token(candidate.get("config_key") or "")]
                )
            )
            linked_core = resolve_ref_entries(candidate_core_refs, candidate_core_refs)
            if any(
                str(entry.get("process_name") or "") == target_proc_name
                for entry in linked_core
            ):
                add_incoming(candidate, "core_service_fields")
                continue
            linked_wait_url = resolve_ref_entries(
                _dep_extract_wait_refs(candidate_cfg.get("wait_for_url")),
                candidate_core_refs,
            )
            if any(
                str(entry.get("process_name") or "") == target_proc_name
                for entry in linked_wait_url
            ):
                add_incoming(candidate, "wait_for_url")
                continue
            linked_wait_dir = resolve_ref_entries(
                _dep_extract_wait_refs(candidate_cfg.get("wait_for_dir")),
                candidate_core_refs,
            )
            if any(
                str(entry.get("process_name") or "") == target_proc_name
                for entry in linked_wait_dir
            ):
                add_incoming(candidate, "wait_for_dir")
                continue
            wait_ports = _dep_extract_wait_ports(candidate_cfg.get("wait_for_url"))
            if target_ports and any(port in target_ports for port in wait_ports):
                add_incoming(candidate, "wait_for_url")
                continue
            for raw_mount in candidate_cfg.get("wait_for_mounts") or []:
                wait_mount = _dep_norm_path(raw_mount)
                if not wait_mount:
                    continue
                if any(
                    wait_mount == mount_path
                    or wait_mount.startswith(mount_path + "/")
                    or mount_path.startswith(wait_mount + "/")
                    for mount_path in target_mounts
                ):
                    add_incoming(candidate, "wait_for_mounts")
                    break
            wait_dirs = _dep_extract_wait_dirs(candidate_cfg.get("wait_for_dir"))
            if wait_dirs and any(
                any(
                    wait_dir == mount_path
                    or wait_dir.startswith(mount_path + "/")
                    or mount_path.startswith(wait_dir + "/")
                    for mount_path in target_mounts
                )
                for wait_dir in wait_dirs
            ):
                add_incoming(candidate, "wait_for_dir")
                continue
            for provider_entry, provider_reason in resolve_rclone_provider_entries(
                candidate, candidate_cfg, (_dep_extract_refs_from_config(candidate_cfg) or [""])[0]
            ):
                if str(provider_entry.get("process_name") or "") == target_proc_name:
                    add_incoming(candidate, provider_reason)
                    break
            candidate_key = _normalize_dep_token(candidate.get("config_key") or "")
            if target_key in [
                _normalize_dep_token(dep)
                for dep in NON_CORE_HARD_DEPENDENCIES.get(candidate_key, [])
            ]:
                add_incoming(candidate, "non_core_dependency_map")
                continue
            candidate_instance_deps = filter_conditional_deps_for_instance(
                conditional_deps, candidate_key, candidate_cfg
            )
            candidate_conditional_deps = candidate_instance_deps.get(candidate_key, set())
            if target_key in {_normalize_dep_token(dep) for dep in candidate_conditional_deps}:
                # For instance-scoped targets, only annotate candidates already
                # detected — conditional_startup_map cannot distinguish instances.
                if target_key not in DEPENDENCY_INSTANCE_SCOPED_KEYS or candidate_name in incoming_map:
                    add_incoming(candidate, "conditional_startup_map")
                continue

        def entry_to_row(entry: dict, reasons: set[str] | None = None) -> dict:
            proc_name = str(entry.get("process_name") or "")
            key = _normalize_dep_token(entry.get("config_key") or "")
            state = _dep_state_for_entries([entry], status_by_process)
            reason_list = sorted(list(reasons or []))
            strengths = {
                DEPENDENCY_SIGNAL_STRENGTH.get(reason, "hard_configured")
                for reason in reason_list
            }
            row_strength = (
                "hard_runtime"
                if "hard_runtime" in strengths
                else "hard_configured"
                if "hard_configured" in strengths
                else "soft_linkage"
            )
            hard = row_strength in {"hard_runtime", "hard_configured"}
            return {
                "process_name": proc_name,
                "key": key,
                "label": entry.get("name") or proc_name or key,
                "state": state,
                "signals": reason_list,
                "classification": "hard" if hard else "linkage",
                "strength": row_strength,
            }

        has_core_deps = target_key in CORE_SERVICE_DEPENDENCIES
        has_non_core_deps = target_key in NON_CORE_HARD_DEPENDENCIES
        has_conditional_deps = target_key in instance_conditional_deps
        context_mode = (
            "core"
            if (has_core_deps or has_non_core_deps or has_conditional_deps)
            else "dependency"
        )
        static_dependencies = CORE_SERVICE_DEPENDENCIES.get(target_key, [])
        if target_key == "decypharr":
            branch_name = _normalize_dep_token(target_config.get("branch") or "")
            mount_type = _normalize_dep_token(target_config.get("mount_type") or "")
            if not mount_type:
                mount_type = "dfs" if branch_name == "beta" else "rclone"
            if mount_type in {"rclone", "dfs", "none"}:
                static_dependencies = [dep for dep in static_dependencies if dep != "rclone"]

        dependency_rows = []
        for dep_key in static_dependencies:
            dep_norm = _normalize_dep_token(dep_key)
            dep_entries = list(by_config_key.get(dep_norm, []))
            if dep_norm in DEPENDENCY_INSTANCE_SCOPED_KEYS:
                dep_entries = [
                    entry
                    for entry in dep_entries
                    if has_core_service(entry.get("config") or {}, target_key)
                ]
            dep_entries = [entry for entry in dep_entries if isinstance(entry, dict)]
            state = _dep_state_for_entries(dep_entries, status_by_process)
            starter = None
            if dep_entries:
                sorted_entries = sorted(
                    dep_entries, key=lambda entry: int(bool(entry.get("enabled"))), reverse=True
                )
                starter = sorted_entries[0]
            dependency_rows.append(
                {
                    "key": dep_norm,
                    "label": CORE_SERVICE_NAMES.get(dep_norm)
                    or (dep_entries[0].get("name") if dep_entries else dep_norm),
                    "state": state,
                    "process_count": len(dep_entries),
                    "scoped": dep_norm in DEPENDENCY_INSTANCE_SCOPED_KEYS,
                    "starter_process_name": starter.get("process_name") if starter else None,
                    "signals": ["core_service_map"],
                    "classification": "hard",
                    "strength": "hard_runtime",
                }
            )

        existing_dep_keys = {row["key"] for row in dependency_rows}
        for dep_key in sorted(instance_conditional_deps.get(target_key, set())):
            dep_norm = _normalize_dep_token(dep_key)
            if dep_norm in existing_dep_keys:
                continue
            dep_entries = [
                entry
                for entry in by_config_key.get(dep_norm, [])
                if isinstance(entry, dict)
            ]
            # For instance-scoped deps, narrow to entries that specific
            # signals already associated with this target instance.
            if dep_norm in DEPENDENCY_INSTANCE_SCOPED_KEYS:
                dep_entries = [
                    e for e in dep_entries
                    if str(e.get("process_name") or "") in outgoing_map
                ]
                if not dep_entries:
                    continue
            state = _dep_state_for_entries(dep_entries, status_by_process)
            starter = None
            if dep_entries:
                sorted_entries = sorted(
                    dep_entries,
                    key=lambda entry: int(bool(entry.get("enabled"))),
                    reverse=True,
                )
                starter = sorted_entries[0]
            dependency_rows.append(
                {
                    "key": dep_norm,
                    "label": CORE_SERVICE_NAMES.get(dep_norm)
                    or (dep_entries[0].get("name") if dep_entries else dep_norm),
                    "state": state,
                    "process_count": len(dep_entries),
                    "scoped": dep_norm in DEPENDENCY_INSTANCE_SCOPED_KEYS,
                    "starter_process_name": starter.get("process_name") if starter else None,
                    "signals": ["conditional_startup_map"],
                    "classification": "hard",
                    "strength": "hard_runtime",
                }
            )
        for dep_key in NON_CORE_HARD_DEPENDENCIES.get(target_key, []):
            dep_norm = _normalize_dep_token(dep_key)
            if dep_norm in existing_dep_keys or dep_norm in {row["key"] for row in dependency_rows}:
                continue
            dep_entries = [
                entry
                for entry in by_config_key.get(dep_norm, [])
                if isinstance(entry, dict)
            ]
            state = _dep_state_for_entries(dep_entries, status_by_process)
            starter = None
            if dep_entries:
                sorted_entries = sorted(
                    dep_entries,
                    key=lambda entry: int(bool(entry.get("enabled"))),
                    reverse=True,
                )
                starter = sorted_entries[0]
            dependency_rows.append(
                {
                    "key": dep_norm,
                    "label": CORE_SERVICE_NAMES.get(dep_norm)
                    or (dep_entries[0].get("name") if dep_entries else dep_norm),
                    "state": state,
                    "process_count": len(dep_entries),
                    "scoped": dep_norm in DEPENDENCY_INSTANCE_SCOPED_KEYS,
                    "starter_process_name": starter.get("process_name") if starter else None,
                    "signals": ["non_core_dependency_map"],
                    "classification": "hard",
                    "strength": "hard_runtime",
                }
            )

        dependent_rows = []
        dependent_keys = [
            core_key
            for core_key, deps in CORE_SERVICE_DEPENDENCIES.items()
            if target_key in [_normalize_dep_token(dep) for dep in deps]
        ]
        if target_key in DEPENDENCY_INSTANCE_SCOPED_KEYS:
            attached_cores = {
                ref for ref in _dep_extract_refs_from_config(target_config) if ref
            }
            if attached_cores:
                dependent_keys = [key for key in dependent_keys if key in attached_cores]

        for core_key in sorted(set(dependent_keys)):
            core_entries = by_config_key.get(core_key, [])
            core_state = _dep_state_for_entries(core_entries, status_by_process)
            core_deps = [
                _normalize_dep_token(dep)
                for dep in CORE_SERVICE_DEPENDENCIES.get(core_key, [])
            ]
            missing = []
            for dep in core_deps:
                dep_entries = by_config_key.get(dep, [])
                if dep in DEPENDENCY_INSTANCE_SCOPED_KEYS:
                    dep_entries = [
                        entry
                        for entry in dep_entries
                        if has_core_service(entry.get("config") or {}, core_key)
                    ]
                if _dep_state_for_entries(dep_entries, status_by_process) != "running":
                    missing.append(dep)
            dependent_rows.append(
                {
                    "key": core_key,
                    "label": CORE_SERVICE_NAMES.get(core_key, core_key),
                    "state": core_state,
                    "missing_deps": missing,
                    "signals": ["core_service_map"],
                    "classification": "hard",
                    "strength": "hard_runtime",
                }
            )

        existing_dependent_keys = {row["key"] for row in dependent_rows}
        for svc_key, svc_deps in conditional_deps.items():
            if target_key not in {_normalize_dep_token(d) for d in svc_deps}:
                continue
            if svc_key in existing_dependent_keys:
                continue
            svc_entries = by_config_key.get(svc_key, [])
            # Instance-filter: for multi-instance services, only include
            # entries whose individual config actually depends on the target.
            if svc_key in DEPENDENCY_INSTANCE_SCOPED_KEYS:
                svc_entries = [
                    entry for entry in svc_entries
                    if target_key in {
                        _normalize_dep_token(d)
                        for d in filter_conditional_deps_for_instance(
                            conditional_deps, svc_key,
                            entry.get("config") if isinstance(entry.get("config"), dict) else {},
                        ).get(svc_key, set())
                    }
                ]
                if not svc_entries:
                    continue
            # When the target is instance-scoped, further narrow to entries
            # that specific signals already associated with this instance.
            if target_key in DEPENDENCY_INSTANCE_SCOPED_KEYS:
                svc_entries = [
                    entry for entry in svc_entries
                    if str(entry.get("process_name") or "") in incoming_map
                ]
                if not svc_entries:
                    continue
            svc_state = _dep_state_for_entries(svc_entries, status_by_process)
            # For instance-scoped services, compute deps from filtered entries
            if svc_key in DEPENDENCY_INSTANCE_SCOPED_KEYS:
                filtered_cond_deps: set[str] = set()
                for entry in svc_entries:
                    entry_cfg = entry.get("config") if isinstance(entry.get("config"), dict) else {}
                    filtered_cond_deps |= filter_conditional_deps_for_instance(
                        conditional_deps, svc_key, entry_cfg
                    ).get(svc_key, set())
            else:
                filtered_cond_deps = conditional_deps.get(svc_key, set())
            all_deps_for_svc = {
                _normalize_dep_token(d)
                for d in (
                    list(CORE_SERVICE_DEPENDENCIES.get(svc_key, []))
                    + list(filtered_cond_deps)
                    + list(NON_CORE_HARD_DEPENDENCIES.get(svc_key, []))
                )
            }
            missing = [
                dep
                for dep in sorted(all_deps_for_svc)
                if _dep_state_for_entries(by_config_key.get(dep, []), status_by_process)
                != "running"
            ]
            dependent_rows.append(
                {
                    "key": svc_key,
                    "label": CORE_SERVICE_NAMES.get(svc_key, svc_key),
                    "state": svc_state,
                    "missing_deps": missing,
                    "signals": ["conditional_startup_map"],
                    "classification": "hard",
                    "strength": "hard_runtime",
                }
            )

        startup_order = []
        if context_mode == "core":
            seen_startup_keys = set()
            for dep in static_dependencies:
                dep_key = _normalize_dep_token(dep)
                if dep_key in seen_startup_keys:
                    continue
                seen_startup_keys.add(dep_key)
                dep_entries = by_config_key.get(dep_key, [])
                if dep_key in DEPENDENCY_INSTANCE_SCOPED_KEYS:
                    dep_entries = [
                        entry
                        for entry in dep_entries
                        if has_core_service(entry.get("config") or {}, target_key)
                    ]
                startup_order.append(
                    {
                        "key": dep_key,
                        "label": CORE_SERVICE_NAMES.get(dep_key, dep_key),
                        "state": _dep_state_for_entries(dep_entries, status_by_process),
                        "signals": ["core_service_map"],
                        "classification": "hard",
                        "strength": "hard_runtime",
                    }
                )
            for dep_key in sorted(instance_conditional_deps.get(target_key, set())):
                dep_norm = _normalize_dep_token(dep_key)
                if dep_norm in seen_startup_keys:
                    continue
                seen_startup_keys.add(dep_norm)
                dep_entries = by_config_key.get(dep_norm, [])
                startup_order.append(
                    {
                        "key": dep_norm,
                        "label": CORE_SERVICE_NAMES.get(dep_norm, dep_norm),
                        "state": _dep_state_for_entries(dep_entries, status_by_process),
                        "signals": ["conditional_startup_map"],
                        "classification": "hard",
                        "strength": "hard_runtime",
                    }
                )
            for dep_key in NON_CORE_HARD_DEPENDENCIES.get(target_key, []):
                dep_norm = _normalize_dep_token(dep_key)
                if dep_norm in seen_startup_keys:
                    continue
                seen_startup_keys.add(dep_norm)
                dep_entries = by_config_key.get(dep_norm, [])
                startup_order.append(
                    {
                        "key": dep_norm,
                        "label": CORE_SERVICE_NAMES.get(dep_norm, dep_norm),
                        "state": _dep_state_for_entries(dep_entries, status_by_process),
                        "signals": ["non_core_dependency_map"],
                        "classification": "hard",
                        "strength": "hard_runtime",
                    }
                )
            startup_order.append(
                {
                    "key": target_key,
                    "label": CORE_SERVICE_NAMES.get(target_key, target_proc_name),
                    "state": _dep_state_for_entries([target], status_by_process),
                    "signals": ["core_service_map"],
                    "classification": "hard",
                    "strength": "hard_runtime",
                }
            )
        elif context_mode == "dependency":
            startup_order.append(
                {
                    "key": target_key,
                    "label": target.get("name") or target_proc_name,
                    "state": _dep_state_for_entries([target], status_by_process),
                    "strength": "hard_configured",
                }
            )
            for row in dependent_rows:
                startup_order.append(
                    {
                        "key": row["key"],
                        "label": row["label"],
                        "state": row["state"],
                        "signals": ["core_service_map"],
                        "classification": "hard",
                        "strength": "hard_runtime",
                    }
                )

        linked_outgoing_rows = [
            entry_to_row(
                entry, outgoing_reasons.get(str(entry.get("process_name") or ""), set())
            )
            for entry in outgoing_map.values()
        ]
        linked_incoming_rows = [
            entry_to_row(
                entry, incoming_reasons.get(str(entry.get("process_name") or ""), set())
            )
            for entry in incoming_map.values()
        ]

        if scope_mode == "all":
            if target_key == "zilean":
                for candidate in processes:
                    candidate_key = _normalize_dep_token(candidate.get("config_key") or "")
                    if candidate_key in ZILEAN_OPTIONAL_LINK_KEYS:
                        name = str(candidate.get("process_name") or "")
                        if not any(row.get("process_name") == name for row in linked_incoming_rows):
                            linked_incoming_rows.append(
                                {
                                    "process_name": name,
                                    "key": candidate_key,
                                    "label": candidate.get("name") or name or candidate_key,
                                    "state": _dep_state_for_entries([candidate], status_by_process),
                                    "signals": ["zilean_optional_integration"],
                                    "classification": "linkage",
                                    "strength": "soft_linkage",
                                }
                            )
            if target_key in ZILEAN_OPTIONAL_LINK_KEYS:
                zilean_entries = by_config_key.get("zilean", [])
                for entry in zilean_entries:
                    name = str(entry.get("process_name") or "")
                    if not any(row.get("process_name") == name for row in linked_outgoing_rows):
                        linked_outgoing_rows.append(
                            {
                                "process_name": name,
                                "key": "zilean",
                                "label": entry.get("name") or name or "zilean",
                                "state": _dep_state_for_entries([entry], status_by_process),
                                "signals": ["zilean_optional_integration"],
                                "classification": "linkage",
                                "strength": "soft_linkage",
                            }
                        )
            doc_links = DOCUMENTED_INTEGRATION_LINKS.get(target_key, [])
            for link_key in doc_links:
                link_norm = _normalize_dep_token(link_key)
                for entry in by_config_key.get(link_norm, []):
                    name = str(entry.get("process_name") or "")
                    if not any(row.get("process_name") == name for row in linked_outgoing_rows):
                        linked_outgoing_rows.append(
                            {
                                "process_name": name,
                                "key": link_norm,
                                "label": entry.get("name") or name or link_key,
                                "state": _dep_state_for_entries([entry], status_by_process),
                                "signals": ["documented_integration"],
                                "classification": "linkage",
                                "strength": "soft_linkage",
                            }
                        )
            for source_key, link_targets in DOCUMENTED_INTEGRATION_LINKS.items():
                if target_key not in [_normalize_dep_token(t) for t in link_targets]:
                    continue
                for entry in by_config_key.get(_normalize_dep_token(source_key), []):
                    name = str(entry.get("process_name") or "")
                    if not any(row.get("process_name") == name for row in linked_incoming_rows):
                        linked_incoming_rows.append(
                            {
                                "process_name": name,
                                "key": _normalize_dep_token(source_key),
                                "label": entry.get("name") or name or source_key,
                                "state": _dep_state_for_entries([entry], status_by_process),
                                "signals": ["documented_integration"],
                                "classification": "linkage",
                                "strength": "soft_linkage",
                            }
                        )

        nodes_map: dict[str, dict] = {}

        def ensure_node(
            process_name_value: str,
            key_value: str = "",
            label_value: str = "",
            state_value: str = "",
        ):
            process_label = str(process_name_value or "").strip()
            if not process_label:
                return
            if process_label in nodes_map:
                return
            normalized_key = _normalize_dep_token(key_value)
            display = (
                str(label_value).strip()
                or CORE_SERVICE_NAMES.get(normalized_key)
                or process_label
            )
            state = state_value or status_by_process.get(process_label) or "unknown"
            nodes_map[process_label] = {
                "id": process_label,
                "process_name": process_label,
                "key": normalized_key,
                "label": display,
                "state": _normalize_dep_token(state) or "unknown",
            }

        ensure_node(
            target_proc_name,
            target_key,
            target.get("name") or CORE_SERVICE_NAMES.get(target_key) or target_proc_name,
            status_by_process.get(target_proc_name) or "unknown",
        )
        linked_outgoing_keys = {
            _normalize_dep_token(row.get("key") or "")
            for row in linked_outgoing_rows
            if row.get("key")
        }
        for row in dependency_rows:
            starter_name = str(row.get("starter_process_name") or "")
            if not starter_name:
                continue
            dep_key = _normalize_dep_token(row.get("key") or "")
            if dep_key in linked_outgoing_keys:
                continue
            ensure_node(
                starter_name,
                row.get("key") or "",
                row.get("label") or starter_name,
                status_by_process.get(starter_name) or "unknown",
            )
        for row in linked_outgoing_rows + linked_incoming_rows:
            ensure_node(
                row.get("process_name") or "",
                row.get("key") or "",
                row.get("label") or "",
                row.get("state") or "",
            )

        edges = []

        def add_edge(source: str, target_name_value: str, signals: list[str]):
            source_name = str(source or "").strip()
            target_name_clean = str(target_name_value or "").strip()
            if not source_name or not target_name_clean:
                return
            strengths = {
                DEPENDENCY_SIGNAL_STRENGTH.get(signal, "hard_configured")
                for signal in signals
            }
            strength = (
                "hard_runtime"
                if "hard_runtime" in strengths
                else "hard_configured"
                if "hard_configured" in strengths
                else "soft_linkage"
            )
            if scope_mode == "runtime" and strength == "soft_linkage":
                return
            edges.append(
                {
                    "source": source_name,
                    "target": target_name_clean,
                    "signals": sorted(set(signals)),
                    "strength": strength,
                    "classification": "hard"
                    if strength in {"hard_runtime", "hard_configured"}
                    else "linkage",
                }
            )

        # Edge direction is dependency -> dependent.
        for row in linked_outgoing_rows:
            add_edge(row.get("process_name"), target_proc_name, row.get("signals") or [])
        for row in linked_incoming_rows:
            add_edge(target_proc_name, row.get("process_name"), row.get("signals") or [])
        for row in dependency_rows:
            starter_name = str(row.get("starter_process_name") or "")
            if not starter_name:
                continue
            dep_key = _normalize_dep_token(row.get("key") or "")
            if dep_key in linked_outgoing_keys:
                continue
            add_edge(starter_name, target_proc_name, row.get("signals") or ["core_service_map"])

        parallel_groups = []
        pre_members = []
        core_members = [target_proc_name] if target_proc_name else []
        post_members = []

        pre_from_static = [
            str(row.get("starter_process_name") or "").strip()
            for row in dependency_rows
            if str(row.get("starter_process_name") or "").strip()
            and _normalize_dep_token(row.get("key") or "") not in linked_outgoing_keys
        ]
        pre_from_links = [
            str(row.get("process_name") or "").strip()
            for row in linked_outgoing_rows
            if str(row.get("strength") or "") in {"hard_runtime", "hard_configured"}
        ]
        pre_members = sorted(set([m for m in pre_from_static + pre_from_links if m and m != target_proc_name]))

        post_members = sorted(
            set(
                [
                    str(row.get("process_name") or "").strip()
                    for row in linked_incoming_rows
                    if str(row.get("process_name") or "").strip()
                ]
            )
        )

        if pre_members:
            parallel_groups.append(
                {
                    "id": "pre_core",
                    "label": "Parallel prerequisites",
                    "type": "parallel",
                    "members": pre_members,
                }
            )
        if core_members:
            parallel_groups.append(
                {
                    "id": "core_target",
                    "label": "Core target",
                    "type": "serial",
                    "members": core_members,
                }
            )
        if post_members:
            parallel_groups.append(
                {
                    "id": "post_core",
                    "label": "Dependents",
                    "type": "parallel",
                    "members": post_members,
                }
            )

        return {
            "process_name": target_proc_name,
            "config_key": target_key,
            "scope": scope_mode,
            "context": {"mode": context_mode, "key": target_key, "core": core_entry},
            "core_services": [
                {
                    "name": CORE_SERVICE_NAMES.get(key, key),
                    "key": key,
                    "dependencies": deps,
                }
                for key, deps in CORE_SERVICE_DEPENDENCIES.items()
            ],
            "processes": processes,
            "statuses": status_by_process,
            "startup_order": startup_order,
            "dependency_rows": dependency_rows,
            "dependent_rows": dependent_rows,
            "linked_outgoing_rows": linked_outgoing_rows,
            "linked_incoming_rows": linked_incoming_rows,
            "nodes": sorted(nodes_map.values(), key=lambda node: node.get("label") or ""),
            "edges": edges,
            "parallel_groups": parallel_groups,
            "dependency_truth_table": DEPENDENCY_TRUTH_TABLE,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to build dependency graph: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to build dependency graph")


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
                process_name,
                enable_update=auto_update_enabled,
                force_update_check=True,
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
            if key in [
                "profilarr",
                "sonarr",
                "radarr",
            ]:
                try:
                    from utils.profilarr_settings import (
                        any_arr_uses_profilarr,
                        patch_profilarr_config,
                    )

                    if key == "profilarr" or any_arr_uses_profilarr():
                        ok, err = patch_profilarr_config()
                        if not ok and err:
                            logger.warning("Profilarr config sync failed: %s", err)
                        if key in ["sonarr", "radarr"]:
                            for attempt in range(2):
                                time.sleep(10)
                                ok, err = patch_profilarr_config()
                                if ok:
                                    break
                                if err:
                                    logger.warning(
                                        "Profilarr config retry %s failed: %s",
                                        attempt + 1,
                                        err,
                                    )
                except Exception as e:
                    logger.warning("Profilarr config sync skipped: %s", e)
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
                # Let updater.auto_update() own setup/start flow to avoid duplicate
                # setup/install passes during restart.
                process_handler.setup_tracker.remove(process_name)

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
            if key in [
                "profilarr",
                "sonarr",
                "radarr",
            ]:
                try:
                    from utils.profilarr_settings import (
                        any_arr_uses_profilarr,
                        patch_profilarr_config,
                    )

                    if key == "profilarr" or any_arr_uses_profilarr():
                        ok, err = patch_profilarr_config()
                        if not ok and err:
                            logger.warning("Profilarr config sync failed: %s", err)
                        if key in ["sonarr", "radarr"]:
                            for attempt in range(2):
                                time.sleep(10)
                                ok, err = patch_profilarr_config()
                                if ok:
                                    break
                                if err:
                                    logger.warning(
                                        "Profilarr config retry %s failed: %s",
                                        attempt + 1,
                                        err,
                                    )
                except Exception as e:
                    logger.warning("Profilarr config sync skipped: %s", e)

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


@process_router.get("/update-status")
def update_status(
    process_name: str = Query(..., description="The name of the process to check"),
    api_state=Depends(get_api_state),
    current_user: str = Depends(get_optional_current_user),
):
    if not process_name:
        raise HTTPException(status_code=400, detail="process_name is required")
    payload = api_state.get_update_status(process_name) if api_state else None
    return {"process_name": process_name, "update_status": payload}


@process_router.post("/update-check")
async def update_check(
    request: UpdateCheckRequest,
    updater=Depends(get_updater),
    api_state=Depends(get_api_state),
    current_user: str = Depends(get_optional_current_user),
):
    if not request.process_name:
        raise HTTPException(status_code=400, detail="process_name is required")
    if not updater:
        raise HTTPException(status_code=500, detail="Updater not available")

    payload = await run_in_threadpool(
        updater.manual_update_check, request.process_name, bool(request.force)
    )
    if api_state and payload:
        api_state.set_update_status(request.process_name, payload)
    return payload


@process_router.post("/update-install")
async def update_install(
    request: UpdateInstallRequest,
    updater=Depends(get_updater),
    api_state=Depends(get_api_state),
    current_user: str = Depends(get_optional_current_user),
):
    if not request.process_name:
        raise HTTPException(status_code=400, detail="process_name is required")
    if not updater:
        raise HTTPException(status_code=500, detail="Updater not available")

    payload = await run_in_threadpool(
        updater.manual_update_install,
        request.process_name,
        bool(request.allow_override),
        request.target,
    )
    if api_state and payload:
        api_state.set_update_status(request.process_name, payload)
    return payload


@process_router.post("/auto-update/reschedule")
async def reschedule_auto_update(
    request: RescheduleAutoUpdateRequest,
    updater=Depends(get_updater),
    api_state=Depends(get_api_state),
    current_user: str = Depends(get_optional_current_user),
):
    if not request.process_name:
        raise HTTPException(status_code=400, detail="process_name is required")
    if not updater:
        raise HTTPException(status_code=500, detail="Updater not available")

    success, message = await run_in_threadpool(
        updater.reschedule_auto_update, request.process_name
    )
    payload = {
        "status": "ok" if success else "error",
        "message": message,
    }
    if api_state:
        api_state.set_update_status(request.process_name, payload)
    return payload


@process_router.get("/symlink-backup-status")
def symlink_backup_status(
    process_name: str = Query(..., description="The name of the process to check"),
    api_state=Depends(get_api_state),
    current_user: str = Depends(get_optional_current_user),
):
    if not process_name:
        raise HTTPException(status_code=400, detail="process_name is required")
    payload = api_state.get_symlink_backup_status(process_name) if api_state else None
    return {"process_name": process_name, "symlink_backup_status": payload}


@process_router.get("/symlink-backup-manifests")
def symlink_backup_manifests(
    process_name: str = Query(..., description="The name of the process to check"),
    current_user: str = Depends(get_optional_current_user),
):
    if not process_name:
        raise HTTPException(status_code=400, detail="process_name is required")
    config = find_service_config(CONFIG_MANAGER.config, process_name)
    if not config:
        raise HTTPException(status_code=404, detail="Process not found")

    template = str(config.get("symlink_backup_path") or "").strip()
    pattern = _symlink_manifest_glob_pattern(process_name, template)
    matches = []
    for path in glob.glob(pattern):
        if not os.path.isfile(path):
            continue
        try:
            stat = os.stat(path)
            matches.append(
                {
                    "path": path,
                    "size_bytes": int(stat.st_size),
                    "modified_at": int(stat.st_mtime),
                }
            )
        except OSError:
            continue

    matches.sort(key=lambda item: item.get("modified_at", 0), reverse=True)
    return {
        "process_name": process_name,
        "pattern": pattern,
        "manifests": matches[:200],
        "count": len(matches),
    }


@process_router.get("/symlink-manifest-files")
def symlink_manifest_files(
    manifest_path: Optional[str] = Query(
        "/config/symlink-repair/snapshots/latest.json",
        description="Manifest path used to resolve directory for listing",
    ),
    current_user: str = Depends(get_optional_current_user),
):
    raw_path = str(manifest_path or "").strip()
    if not raw_path:
        raw_path = "/config/symlink-repair/snapshots/latest.json"
    directory = os.path.dirname(raw_path) or "."

    entries = []
    try:
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            try:
                stat = os.stat(path)
                entries.append(
                    {
                        "path": path,
                        "name": name,
                        "size_bytes": int(stat.st_size),
                        "modified_at": int(stat.st_mtime),
                    }
                )
            except OSError:
                continue
    except FileNotFoundError:
        return {
            "directory": directory,
            "manifest_path": raw_path,
            "files": [],
            "count": 0,
        }
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list manifest files in '{directory}': {e}",
        )

    entries.sort(key=lambda item: item.get("modified_at", 0), reverse=True)
    return {
        "directory": directory,
        "manifest_path": raw_path,
        "files": entries[:500],
        "count": len(entries),
    }


@process_router.post("/symlink-backup/reschedule")
async def reschedule_symlink_backup(
    request: RescheduleSymlinkBackupRequest,
    updater=Depends(get_updater),
    current_user: str = Depends(get_optional_current_user),
):
    if not request.process_name:
        raise HTTPException(status_code=400, detail="process_name is required")
    if not updater:
        raise HTTPException(status_code=500, detail="Updater not available")

    success, message = await run_in_threadpool(
        updater.reschedule_symlink_backup, request.process_name
    )
    payload = {
        "status": "ok" if success else "error",
        "message": message,
    }
    return payload


@process_router.post("/symlink-repair")
async def symlink_repair(
    request: SymlinkRepairRequest,
    current_user: str = Depends(get_optional_current_user),
):
    from utils.symlink_repair import repair_symlinks

    rules_payload = (
        [rule.model_dump() for rule in request.rewrite_rules]
        if request.rewrite_rules
        else []
    )
    root_migrations_payload = (
        [migration.model_dump() for migration in request.root_migrations]
        if request.root_migrations
        else []
    )
    if not rules_payload and not request.presets and not root_migrations_payload:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one rewrite rule, root migration, or preset.",
        )

    try:
        report = await run_in_threadpool(
            repair_symlinks,
            request.roots,
            rules_payload,
            bool(request.dry_run),
            bool(request.include_broken),
            request.backup_path,
            request.presets,
            root_migrations_payload,
            bool(request.overwrite_existing),
            bool(request.copy_instead_of_move),
        )
        return report
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Symlink repair failed: {e}")


@process_router.post("/symlink-repair-async")
async def symlink_repair_async(
    request: SymlinkRepairRequest,
    api_state=Depends(get_api_state),
    current_user: str = Depends(get_optional_current_user),
):
    from utils.symlink_repair import repair_symlinks

    if not api_state:
        raise HTTPException(status_code=500, detail="API state unavailable")

    rules_payload = (
        [rule.model_dump() for rule in request.rewrite_rules]
        if request.rewrite_rules
        else []
    )
    root_migrations_payload = (
        [migration.model_dump() for migration in request.root_migrations]
        if request.root_migrations
        else []
    )
    if not rules_payload and not request.presets and not root_migrations_payload:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one rewrite rule, root migration, or preset.",
        )

    process_name = str(request.process_name or "symlink-repair").strip()
    job_payload = api_state.create_symlink_job(
        process_name=process_name,
        operation="symlink_repair",
        metadata={
            "dry_run": bool(request.dry_run),
            "include_broken": bool(request.include_broken),
            "backup_path": request.backup_path,
            "presets": request.presets or [],
            "rewrite_rules": rules_payload,
            "root_migrations": root_migrations_payload,
            "overwrite_existing": bool(request.overwrite_existing),
            "copy_instead_of_move": bool(request.copy_instead_of_move),
            "roots": request.roots or [],
        },
    )
    job_id = job_payload["job_id"]

    def run_job():
        api_state.update_symlink_job(
            job_id,
            {
                "status": "running",
                "started_at": int(time.time()),
                "progress": {
                    "stage": "collecting",
                    "processed_items": 0,
                    "total_items": None,
                    "changed": 0,
                    "moved": 0,
                    "copied": 0,
                    "errors": 0,
                },
            },
        )
        try:

            def progress_callback(payload):
                api_state.update_symlink_job(
                    job_id,
                    {
                        "progress": payload,
                    },
                )

            report = repair_symlinks(
                request.roots,
                rules_payload,
                bool(request.dry_run),
                bool(request.include_broken),
                request.backup_path,
                request.presets,
                root_migrations_payload,
                bool(request.overwrite_existing),
                bool(request.copy_instead_of_move),
                progress_callback,
            )
            api_state.update_symlink_job(
                job_id,
                {
                    "status": "completed",
                    "finished_at": int(time.time()),
                    "result": report,
                    "error": None,
                },
            )
        except Exception as e:
            api_state.update_symlink_job(
                job_id,
                {
                    "status": "error",
                    "finished_at": int(time.time()),
                    "error": {"message": str(e)},
                },
            )

    thread = threading.Thread(target=run_job, daemon=True)
    thread.start()

    return {
        "status": "queued",
        "job_id": job_id,
        "operation": "symlink_repair",
    }


@process_router.post("/symlink-manifest/backup")
async def symlink_manifest_backup(
    request: SymlinkManifestBackupRequest,
    current_user: str = Depends(get_optional_current_user),
):
    from utils.symlink_repair import backup_symlink_manifest

    try:
        report = await run_in_threadpool(
            backup_symlink_manifest,
            request.roots,
            request.backup_path,
            bool(request.include_broken),
        )
        return report
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Symlink manifest backup failed: {e}"
        )


@process_router.post("/symlink-manifest/backup-async")
async def symlink_manifest_backup_async(
    request: SymlinkManifestBackupRequest,
    api_state=Depends(get_api_state),
    current_user: str = Depends(get_optional_current_user),
):
    from utils.symlink_repair import backup_symlink_manifest

    if not api_state:
        raise HTTPException(status_code=500, detail="API state unavailable")

    process_name = str(request.process_name or "symlink-manifest").strip()
    job_payload = api_state.create_symlink_job(
        process_name=process_name,
        operation="symlink_manifest_backup",
        metadata={
            "backup_path": request.backup_path,
            "include_broken": bool(request.include_broken),
            "roots": request.roots or [],
        },
    )
    job_id = job_payload["job_id"]

    def run_job():
        api_state.update_symlink_job(
            job_id,
            {
                "status": "running",
                "started_at": int(time.time()),
                "progress": {
                    "stage": "collecting",
                    "processed_symlinks": 0,
                    "total_symlinks": None,
                    "recorded_entries": 0,
                    "errors": 0,
                },
            },
        )
        try:
            def progress_callback(payload):
                api_state.update_symlink_job(
                    job_id,
                    {
                        "progress": payload,
                    },
                )

            report = backup_symlink_manifest(
                request.roots,
                request.backup_path,
                bool(request.include_broken),
                progress_callback,
            )
            api_state.update_symlink_job(
                job_id,
                {
                    "status": "completed",
                    "finished_at": int(time.time()),
                    "result": report,
                    "error": None,
                },
            )
        except Exception as e:
            api_state.update_symlink_job(
                job_id,
                {
                    "status": "error",
                    "finished_at": int(time.time()),
                    "error": {"message": str(e)},
                },
            )

    thread = threading.Thread(target=run_job, daemon=True)
    thread.start()

    return {
        "status": "queued",
        "job_id": job_id,
        "operation": "symlink_manifest_backup",
    }


@process_router.get("/symlink-job-status")
def symlink_job_status(
    job_id: str = Query(..., description="Symlink job ID"),
    api_state=Depends(get_api_state),
    current_user: str = Depends(get_optional_current_user),
):
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id is required")
    if not api_state:
        raise HTTPException(status_code=500, detail="API state unavailable")
    payload = api_state.get_symlink_job(job_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Symlink job not found")
    return payload


@process_router.get("/symlink-job-latest")
def symlink_job_latest(
    process_name: str = Query(..., description="Service process name"),
    operation: Optional[str] = Query(
        "symlink_manifest_backup", description="Symlink job operation"
    ),
    active_only: bool = Query(
        True, description="If true, return only queued/running jobs"
    ),
    api_state=Depends(get_api_state),
    current_user: str = Depends(get_optional_current_user),
):
    if not process_name:
        raise HTTPException(status_code=400, detail="process_name is required")
    if not api_state:
        raise HTTPException(status_code=500, detail="API state unavailable")
    payload = api_state.get_latest_symlink_job(process_name, operation, active_only)
    return {"job": payload}


@process_router.post("/symlink-manifest/restore")
async def symlink_manifest_restore(
    request: SymlinkManifestRestoreRequest,
    current_user: str = Depends(get_optional_current_user),
):
    from utils.symlink_repair import restore_symlink_manifest

    try:
        report = await run_in_threadpool(
            restore_symlink_manifest,
            request.manifest_path,
            bool(request.dry_run),
            bool(request.overwrite_existing),
            bool(request.restore_broken),
        )
        return report
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Symlink manifest restore failed: {e}"
        )


@process_router.get("/symlink-manifest/compare")
async def symlink_manifest_compare(
    manifest_path: str = Query(..., description="Snapshot manifest path to compare"),
    overwrite_existing: bool = Query(
        False, description="If true, preview assumes existing paths can be overwritten"
    ),
    restore_broken: bool = Query(
        True, description="If false, preview skips entries with missing targets"
    ),
    sample_limit: int = Query(
        50, ge=0, le=200, description="Maximum sample entries to return"
    ),
    current_user: str = Depends(get_optional_current_user),
):
    from utils.symlink_repair import preview_symlink_manifest_restore

    try:
        report = await run_in_threadpool(
            preview_symlink_manifest_restore,
            manifest_path,
            bool(overwrite_existing),
            bool(restore_broken),
            int(sample_limit),
        )
        return report
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Symlink manifest compare failed: {e}"
        )


@process_router.post("/symlink-manifest/restore-async")
async def symlink_manifest_restore_async(
    request: SymlinkManifestRestoreRequest,
    api_state=Depends(get_api_state),
    current_user: str = Depends(get_optional_current_user),
):
    from utils.symlink_repair import restore_symlink_manifest

    if not api_state:
        raise HTTPException(status_code=500, detail="API state unavailable")

    process_name = str(request.process_name or request.manifest_path or "symlink-manifest").strip()
    job_payload = api_state.create_symlink_job(
        process_name=process_name,
        operation="symlink_manifest_restore",
        metadata={
            "manifest_path": request.manifest_path,
            "dry_run": bool(request.dry_run),
            "overwrite_existing": bool(request.overwrite_existing),
            "restore_broken": bool(request.restore_broken),
        },
    )
    job_id = job_payload["job_id"]

    def run_job():
        api_state.update_symlink_job(
            job_id,
            {
                "status": "running",
                "started_at": int(time.time()),
                "progress": {
                    "stage": "processing",
                    "processed_entries": 0,
                    "total_entries": None,
                    "restored": 0,
                    "errors": 0,
                },
            },
        )
        try:
            def progress_callback(payload):
                api_state.update_symlink_job(
                    job_id,
                    {
                        "progress": payload,
                    },
                )

            report = restore_symlink_manifest(
                request.manifest_path,
                bool(request.dry_run),
                bool(request.overwrite_existing),
                bool(request.restore_broken),
                progress_callback,
            )
            api_state.update_symlink_job(
                job_id,
                {
                    "status": "completed",
                    "finished_at": int(time.time()),
                    "result": report,
                    "error": None,
                },
            )
        except Exception as e:
            api_state.update_symlink_job(
                job_id,
                {
                    "status": "error",
                    "finished_at": int(time.time()),
                    "error": {"message": str(e)},
                },
            )

    thread = threading.Thread(target=run_job, daemon=True)
    thread.start()

    return {
        "status": "queued",
        "job_id": job_id,
        "operation": "symlink_manifest_restore",
    }


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


def _normalize_process_slug(value: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(value or ""))
    slug = slug.strip("-")
    return slug or "service"


def _symlink_manifest_glob_pattern(process_name: str, template: str) -> str:
    pattern = str(template or "").strip()
    if not pattern:
        pattern = "/config/symlink-repair/snapshots/{process_slug}-{timestamp}.json"
    replacements = {
        "{timestamp}": "*",
        "{date}": "*",
        "{time}": "*",
        "{process_name}": process_name,
        "{process_slug}": _normalize_process_slug(process_name),
    }
    for token, value in replacements.items():
        pattern = pattern.replace(token, value)
    return pattern


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


def _normalize_dep_token(value: Any) -> str:
    token = str(value or "").strip().lower()
    if not token:
        return ""
    return normalize_identifier(token)


def _dep_state_for_entries(entries: list[dict], status_by_process: dict[str, str]) -> str:
    if not entries:
        return "missing"
    has_running = any(
        str(status_by_process.get(str(entry.get("process_name")), "")).lower()
        == "running"
        for entry in entries
    )
    if has_running:
        return "running"
    has_enabled = any(entry.get("enabled", False) for entry in entries)
    if has_enabled:
        return "stopped"
    return "disabled"


def _dep_process_ports(process_entry: dict) -> list[int]:
    config = process_entry.get("config") if isinstance(process_entry, dict) else {}
    if not isinstance(config, dict):
        return []
    ports = []
    for key in (
        "port",
        "frontend_port",
        "backend_port",
        "web_port",
        "ui_port",
        "http_port",
        "https_port",
        "external_port",
    ):
        value = config.get(key)
        if isinstance(value, int) and value > 0:
            ports.append(value)
    if isinstance(config.get("ports"), list):
        for value in config.get("ports", []):
            if isinstance(value, int) and value > 0:
                ports.append(value)
    return sorted(set(ports))


def _dep_is_local_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    return normalized in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


def _dep_norm_path(path: str) -> str:
    value = os.path.normpath(str(path or "").strip())
    if value == ".":
        return ""
    return value.rstrip("/") or "/"


def _dep_extract_refs_from_config(config: dict) -> list[str]:
    refs: list[str] = []

    def add_value(value: Any):
        if value is None:
            return
        if isinstance(value, str):
            parts = [_normalize_dep_token(entry) for entry in value.split(",")]
            refs.extend([entry for entry in parts if entry])
            return
        if isinstance(value, list):
            for item in value:
                add_value(item)

    if not isinstance(config, dict):
        return refs
    add_value(config.get("core_services"))
    add_value(config.get("core_service"))
    return sorted(set(refs))


def _dep_extract_wait_refs(wait_entries: Any) -> list[str]:
    refs: list[str] = []

    def add_value(value: Any):
        if value is None:
            return
        if isinstance(value, str):
            parts = [_normalize_dep_token(entry) for entry in value.split(",")]
            refs.extend([entry for entry in parts if entry])
            return
        if isinstance(value, list):
            for item in value:
                add_value(item)

    if not isinstance(wait_entries, list):
        return refs
    for entry in wait_entries:
        if not isinstance(entry, dict):
            continue
        add_value(entry.get("core_service"))
        add_value(entry.get("core_services"))
        add_value(entry.get("service"))
        add_value(entry.get("process_name"))
        raw_url = str(entry.get("url") or "").strip()
        if not raw_url:
            continue
        try:
            parsed = re.match(r"^[a-zA-Z]+://", raw_url)
            if not parsed:
                continue
            from urllib.parse import urlparse, unquote

            parsed_url = urlparse(raw_url)
            host = str(parsed_url.hostname or "").strip().lower()
            if host and not _dep_is_local_host(host):
                add_value(host)
            path_match = re.match(r"^/ui/([^/]+)", parsed_url.path or "", re.I)
            if path_match:
                add_value(unquote(path_match.group(1)))
        except Exception:
            continue
    return sorted(set(refs))


def _dep_extract_wait_ports(wait_entries: Any) -> list[int]:
    ports: list[int] = []
    if not isinstance(wait_entries, list):
        return ports
    for entry in wait_entries:
        if not isinstance(entry, dict):
            continue
        raw_url = str(entry.get("url") or "").strip()
        if not raw_url:
            continue
        try:
            from urllib.parse import urlparse

            parsed_url = urlparse(raw_url)
            if not _dep_is_local_host(parsed_url.hostname or ""):
                continue
            if parsed_url.port and int(parsed_url.port) > 0:
                ports.append(int(parsed_url.port))
        except Exception:
            continue
    return sorted(set(ports))


def _dep_extract_wait_dirs(wait_value: Any) -> list[str]:
    paths: list[str] = []

    def add_value(value: Any):
        if value is None:
            return
        if isinstance(value, str):
            normalized = _dep_norm_path(value)
            if normalized:
                paths.append(normalized)
            return
        if isinstance(value, list):
            for item in value:
                add_value(item)
            return
        if isinstance(value, dict):
            for key in ("path", "dir", "directory", "value"):
                if key in value:
                    add_value(value.get(key))

    add_value(wait_value)
    return sorted(set([path for path in paths if path]))


def _collect_process_entries() -> list[dict]:
    processes: list[dict] = []
    config = CONFIG_MANAGER.config

    def find_processes(data, parent_key=""):
        if not isinstance(data, dict):
            return
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
    return processes


def _dep_process_mount_points(process_entry: dict) -> list[str]:
    config = process_entry.get("config") if isinstance(process_entry, dict) else {}
    if not isinstance(config, dict):
        return []
    points: list[str] = []

    mount_path = _dep_norm_path(config.get("mount_path") or "")
    if mount_path:
        points.append(mount_path)

    mount_dir = _dep_norm_path(config.get("mount_dir") or "")
    mount_name = str(config.get("mount_name") or "").strip().strip("/")
    if mount_dir and mount_name:
        points.append(_dep_norm_path(os.path.join(mount_dir, mount_name)))

    return sorted(set([point for point in points if point]))


def _reserve_port(
    used_ports: dict[int, str],
    desired: int,
    service_key: str,
    owner: str,
    logger,
    label: str = "port",
    allow_in_use_for_owner: bool = False,
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
    if existing_owner and existing_owner == owner and allow_in_use_for_owner:
        used_ports[desired] = owner
        return desired
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
    allow_in_use_for_owner: bool = False,
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
        allow_in_use_for_owner=allow_in_use_for_owner,
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
            proc = inst_cfg.get("process_name")
            if not proc:
                raise HTTPException(
                    500, detail=f"Process name not defined for '{opt_key}:{inst_name}'."
                )
            is_running = api_state.get_status(proc) == "running"
            if not is_running:
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
                allow_in_use_for_owner=is_running,
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
    proc = opt_cfg.get("process_name")
    is_running = api_state.get_status(proc) == "running" if proc else False
    _reserve_config_port(
        opt_key, opt_cfg, "port", used_ports, logger, allow_in_use_for_owner=is_running
    )
    if opt_key == "nzbdav":
        _reserve_config_port(
            "nzbdav",
            opt_cfg,
            "frontend_port",
            used_ports,
            logger,
            label="frontend",
            allow_in_use_for_owner=is_running,
        )
        _reserve_config_port(
            "nzbdav",
            opt_cfg,
            "backend_port",
            used_ports,
            logger,
            label="backend",
            allow_in_use_for_owner=is_running,
        )

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
            proc_name = cfg["process_name"]
            is_running = api_state.get_status(proc_name) == "running"
            _reserve_config_port(
                ident,
                cfg,
                "port",
                used_ports,
                logger,
                allow_in_use_for_owner=is_running,
            )
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
        pg_name = pg["process_name"]
        is_running = api_state.get_status(pg_name) == "running"
        _reserve_config_port(
            "postgres",
            pg,
            "port",
            used_ports,
            logger,
            allow_in_use_for_owner=is_running,
        )
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
            branch_name = (
                effective_opts.get(
                    "branch",
                    (config.get(config_key, {}) or {}).get("branch"),
                )
                or ""
            )
            beta_enabled = str(branch_name).strip().lower() == "beta"
            mount_type = (
                effective_opts.get(
                    "mount_type",
                    (config.get(config_key, {}) or {}).get("mount_type"),
                )
                or ""
            )
            mount_type = str(mount_type).strip().lower()
            if beta_enabled and not mount_type:
                mount_type = "dfs"
            if not mount_type and not beta_enabled:
                mount_type = "rclone"

            if config_key == "decypharr" and beta_enabled:
                # Beta builds use branch deployments; default to beta unless overridden
                desired_branch = effective_opts.get("branch") or "beta"
                cfg = config.get(config_key, {}) or {}
                updated = False
                if not cfg.get("branch_enabled"):
                    cfg["branch_enabled"] = True
                    updated = True
                if (cfg.get("branch") or "").strip() != desired_branch:
                    cfg["branch"] = desired_branch
                    updated = True
                if cfg.get("release_version_enabled"):
                    cfg["release_version_enabled"] = False
                    updated = True
                if cfg.get("mount_type") != mount_type:
                    cfg["mount_type"] = mount_type
                    updated = True
                if updated:
                    config[config_key] = cfg
                    CONFIG_MANAGER.save_config()

            # If decypharr uses embedded rclone, drop rclone from deps *now*
            if config_key == "decypharr" and mount_type in (
                "rclone",
                "dfs",
                "none",
                "",
            ):
                logger.debug(
                    "Decypharr does not require DUMB rclone; removing 'rclone' from dependencies."
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

                    proc_name = inst_cfg.get("process_name")
                    if not proc_name:
                        raise HTTPException(
                            500,
                            detail=f"Process name not defined for '{config_key}:{inst_name}'",
                        )
                    is_running = api_state.get_status(proc_name) == "running"
                    if not is_running:
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
                        allow_in_use_for_owner=is_running,
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
                proc_name = core_cfg["process_name"]
                is_running = api_state.get_status(proc_name) == "running"
                _reserve_config_port(
                    config_key,
                    core_cfg,
                    "port",
                    used_ports,
                    logger,
                    allow_in_use_for_owner=is_running,
                )
                if config_key == "nzbdav":
                    _reserve_config_port(
                        "nzbdav",
                        core_cfg,
                        "frontend_port",
                        used_ports,
                        logger,
                        label="frontend",
                        allow_in_use_for_owner=is_running,
                    )
                    _reserve_config_port(
                        "nzbdav",
                        core_cfg,
                        "backend_port",
                        used_ports,
                        logger,
                        label="backend",
                        allow_in_use_for_owner=is_running,
                    )
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

    try:
        from utils.profilarr_settings import (
            any_arr_uses_profilarr,
            patch_profilarr_config,
        )

        if any_arr_uses_profilarr():
            ok, err = patch_profilarr_config()
            if not ok and err:
                logger.warning("Profilarr config sync failed: %s", err)
            for attempt in range(2):
                time.sleep(10)
                ok, err = patch_profilarr_config()
                if ok:
                    break
                if err:
                    logger.warning(
                        "Profilarr config retry %s failed: %s", attempt + 1, err
                    )
    except Exception as exc:
        logger.warning("Profilarr auto-config skipped: %s", exc)

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
        if key in ONBOARDING_HIDDEN_CORE_SERVICES:
            continue
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
        "manual_update_check": True,
        "seerr_sync": True,
        "auto_update_start_time": True,
        "symlink_repair": True,
        "symlink_repair_async": True,
        "symlink_manifest_backup": True,
        "symlink_manifest_backup_async": True,
        "symlink_job_status": True,
        "symlink_job_latest": True,
        "symlink_manifest_restore": True,
        "symlink_manifest_restore_async": True,
        "symlink_manifest_compare": True,
        "symlink_backup_schedule": True,
        "symlink_backup_manifest_list": True,
        "symlink_manifest_file_list": True,
    }
