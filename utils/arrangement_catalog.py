"""Backend-owned setup roles and human tasks, independent of process startup."""

from __future__ import annotations

ROLES = {
    "access-security": (
        "Access and security",
        ("authelia", "cloudflared", "traefik", "traefik_proxy_admin"),
    ),
    "databases-administration": (
        "Databases and administration",
        ("postgres", "pgadmin", "phalanx_db"),
    ),
    "download-workflows": (
        "Download workflows",
        (
            "decypharr",
            "infinidysk",
            "altmount",
            "riven_backend",
            "cli_debrid",
            "cli_battery",
            "rclone",
            "zurg",
        ),
    ),
    "library-automation": (
        "Library automation",
        (
            "radarr",
            "sonarr",
            "lidarr",
            "whisparr",
            "neutarr",
            "profilarr",
            "bazarr",
            "maintainerr",
        ),
    ),
    "media-playback": (
        "Media servers and playback",
        ("plex", "jellyfin", "emby", "audiobookshelf", "mediastorm", "aiostreams"),
    ),
    "requests-discovery": (
        "Requests and discovery",
        ("seerr", "pulsarr", "prowlarr", "zilean"),
    ),
    "utilities-interfaces": (
        "Utilities and interfaces",
        ("riven_frontend", "tautulli"),
    ),
}

CORE_KEYS = frozenset(
    (
        "plex",
        "jellyfin",
        "emby",
        "cli_debrid",
        "decypharr",
        "infinidysk",
        "altmount",
        "riven_backend",
        "radarr",
        "sonarr",
        "lidarr",
        "prowlarr",
        "whisparr",
        "seerr",
        "neutarr",
        "profilarr",
    )
)
OPTIONAL_KEYS = frozenset(
    (
        "zilean",
        "pgadmin",
        "postgres",
        "riven_frontend",
        "tautulli",
        "bazarr",
        "pulsarr",
        "maintainerr",
        "aiostreams",
        "audiobookshelf",
        "mediastorm",
        "traefik_proxy_admin",
        "authelia",
        "cloudflared",
    )
)
ARR_KEYS = frozenset(("radarr", "sonarr", "lidarr", "whisparr"))
MEDIA_KEYS = frozenset(("plex", "jellyfin", "emby", "audiobookshelf"))
PROVIDER_KEYS = frozenset(("decypharr", "infinidysk", "altmount"))
SERVICE_NAMES = {
    "aiostreams": "AIOStreams",
    "altmount": "AltMount",
    "audiobookshelf": "Audiobookshelf",
    "authelia": "Authelia",
    "bazarr": "Bazarr",
    "cli_battery": "CLI Battery",
    "cli_debrid": "CLI Debrid",
    "cloudflared": "Cloudflared",
    "decypharr": "Decypharr",
    "emby": "Emby",
    "infinidysk": "InfiniDysk",
    "jellyfin": "Jellyfin",
    "lidarr": "Lidarr",
    "maintainerr": "Maintainerr",
    "mediastorm": "mediastorm",
    "neutarr": "NeutArr",
    "pgadmin": "pgAdmin",
    "phalanx_db": "PhalanxDB",
    "plex": "Plex",
    "postgres": "PostgreSQL",
    "profilarr": "Profilarr",
    "prowlarr": "Prowlarr",
    "pulsarr": "Pulsarr",
    "radarr": "Radarr",
    "rclone": "rclone",
    "riven_backend": "Riven Backend",
    "riven_frontend": "Riven Frontend",
    "seerr": "Seerr",
    "sonarr": "Sonarr",
    "tautulli": "Tautulli",
    "traefik": "Traefik",
    "traefik_proxy_admin": "Traefik Proxy Admin",
    "whisparr": "Whisparr",
    "zilean": "Zilean",
    "zurg": "Zurg",
}

# These are actual user tasks. A process becoming ready never completes them.
# (id, label, actionable instructions, required)
MANUAL_TASKS = {
    "plex": (
        (
            "account",
            "Authorize Plex",
            "Claim this server with your Plex account. An existing claimed server is reused; request a fresh claim only when required.",
            True,
        ),
        (
            "sharing",
            "Review Plex sharing and playback",
            "Choose users, sharing, transcoding and remote access in Plex. Enabling Plex does not authorize public access.",
            False,
        ),
    ),
    "jellyfin": (
        (
            "account",
            "Create the Jellyfin administrator",
            "Complete the initial administrator setup in Jellyfin, or provide authorized setup credentials below for a fresh server.",
            True,
        ),
        (
            "protection",
            "Connect Jellyfin library protection",
            "Authorize the dedicated API key for DUMB Library Protection and recheck authenticated session/library access. Account setup alone does not prove outage protection.",
            True,
        ),
        (
            "devices",
            "Choose playback and hardware acceleration",
            "Review playback settings. Host GPU devices and Docker device mappings must be supplied by the operator.",
            False,
        ),
    ),
    "emby": (
        (
            "account",
            "Complete Emby first setup",
            "Open Emby and create the first administrator. DUMB never initializes an existing server again.",
            True,
        ),
        (
            "protection",
            "Connect Emby library protection",
            "Create a dedicated Emby API key and save it in the DUMB Library Protection panel. Recheck the authenticated connection.",
            True,
        ),
    ),
    "audiobookshelf": (
        (
            "account",
            "Create the Audiobookshelf root account",
            "Open Audiobookshelf and create its first root account, or use the fresh-server setup action below.",
            True,
        ),
    ),
    "infinidysk": (
        (
            "indexers",
            "Configure your Usenet indexers",
            "Add the indexers you own to the selected Prowlarr instance, or configure them in your Arr applications. Provider accounts and indexer accounts are separate.",
            True,
        ),
    ),
    "decypharr": (
        (
            "indexers",
            "Choose and authenticate indexers",
            "Review the selected Prowlarr feeds and add any required private indexer credentials. Conditional Zilean/StremThru feeds do not configure your Usenet indexer accounts.",
            True,
        ),
    ),
    "altmount": (
        (
            "account",
            "Authorize the AltMount administrator",
            "Authorize an existing administrator or explicitly create the first account on a recognized fresh server. Secure WebDAV access in its separate checklist task.",
            True,
        ),
        (
            "import",
            "Choose AltMount import strategy",
            "Choose direct mount, symlink or STRM behavior; configure the import directory and matching Arr/media-library roots. External rclone mode requires a separately provided mount.",
            True,
        ),
    ),
    "cli_debrid": (
        (
            "native_setup",
            "Complete CLI Debrid application setup",
            "Open CLI Debrid and select content sources, collection mode, original/symlink paths and scraper choices. Review the selected source's Battery topology.",
            True,
        ),
    ),
    "riven_backend": (
        (
            "content",
            "Select a Riven content source",
            "Select at least one content source and the required scrapers in Riven. Review ranking and library policy for the selected release/source mode.",
            True,
        ),
    ),
    "riven_frontend": (
        (
            "origin",
            "Confirm the Riven browser origin",
            "Use the browser-facing origin you intend to access. Backend API credentials and connection configuration are checked separately.",
            True,
        ),
    ),
    "prowlarr": (
        (
            "indexers",
            "Add your indexer accounts",
            "Add and test the indexers you own. Select their tags/categories and application synchronization policy for this arrangement.",
            True,
        ),
    ),
    "profilarr": (
        (
            "profiles",
            "Choose and apply Profilarr profiles",
            "Choose a profile database and desired profiles/custom formats. Preview naming and quality changes and run the selected sync. Connections alone do not install the desired profiles.",
            True,
        ),
    ),
    "neutarr": (
        (
            "policy",
            "Review NeutArr searches",
            "Choose backlog-search schedules, limits and upgrade policy before enabling unattended searches.",
            True,
        ),
    ),
    "bazarr": (
        (
            "languages",
            "Choose subtitle languages and providers",
            "Choose languages/profiles and configure subtitle-provider credentials in Bazarr. Arr connections are tracked separately.",
            True,
        ),
    ),
    "maintainerr": (
        (
            "rules",
            "Review Maintainerr rules",
            "Preview collections and rules. Enabling connections never enables deletion, unmonitoring or retention actions on your behalf.",
            True,
        ),
    ),
    "seerr": (
        (
            "account",
            "Sign in to Seerr",
            "Authorize the selected media server and administrator in Seerr. Select libraries and request approval/user policy. Arr routing is tracked separately.",
            True,
        ),
    ),
    "pulsarr": (
        (
            "account",
            "Create the Pulsarr administrator",
            "Open Pulsarr and complete its administrator setup. Plex/watchlist authorization can require separate account permissions.",
            True,
        ),
        (
            "metadata",
            "Configure Pulsarr metadata access",
            "For versions using tmdbApiKey, supply your TMDB API Read Access Token in Pulsarr environment settings. This enables metadata features; it is not a generic startup prerequisite.",
            True,
        ),
        (
            "watchlists",
            "Review and activate Pulsarr watchlists",
            "Choose friend visibility, profile/root/default routing and notifications, then explicitly start the workflow or enable autostart.",
            True,
        ),
    ),
    "tautulli": (
        (
            "account",
            "Complete Tautulli first setup",
            "Complete the Tautulli administrator wizard. The Plex connection action preserves existing nonempty settings.",
            True,
        ),
        (
            "notifications",
            "Choose Tautulli notifications",
            "Select the events and recipients you want; no notification recipients are assumed.",
            False,
        ),
    ),
    "mediastorm": (
        (
            "account",
            "Secure the mediastorm administrator",
            "Open mediastorm and replace its initial password during first sign-in. The service page retains compatible bootstrap guidance.",
            True,
        ),
        (
            "providers",
            "Configure mediastorm metadata and providers",
            "Add your metadata credentials and selected Debrid, torrent or Usenet providers in mediastorm.",
            True,
        ),
    ),
    "aiostreams": (
        (
            "addons",
            "Configure addons and install in Stremio",
            "Choose your addons/provider preferences. Use the final public HTTPS origin, create its route, then install the addon in Stremio. An embedded UI address is not the public addon origin.",
            True,
        ),
    ),
    "authelia": (
        (
            "bootstrap",
            "Complete managed Authelia setup",
            "Open the Authelia service page: set the public URL/domain, administrator and notifier, then bootstrap. Its pre-bootstrap stopped state is expected.",
            True,
        ),
        (
            "enrollment",
            "Enroll the first authenticator",
            "Open the real Authelia portal and complete identity verification and factor enrollment. Then explicitly choose OIDC/ForwardAuth integrations.",
            True,
        ),
    ),
    "traefik_proxy_admin": (
        (
            "account",
            "Create the TPA administrator",
            "Create the first TPA administrator, then choose the domains/routes and protection policy you want.",
            True,
        ),
    ),
    "cloudflared": (
        (
            "public_route",
            "Configure and check Cloudflare routes",
            "Create the tunnel/domain/DNS and published application route in Cloudflare. A connector token does not grant DNS/account provisioning. Match the chosen HTTPS origin and TPA route.",
            True,
        ),
    ),
    "pgadmin": (
        (
            "login",
            "Connect pgAdmin to PostgreSQL",
            "Sign in with your configured pgAdmin account and enter the PostgreSQL connection password on first connection. Choose backup/retention separately.",
            True,
        ),
    ),
    "zilean": (
        (
            "population",
            "Wait for initial Zilean index population",
            "API readiness does not establish searchable content. Review upstream ingestion progress; initial population can take a long time.",
            True,
        ),
    ),
    "zurg": (
        (
            "scan",
            "Choose Zurg library-update behavior",
            "The copied Plex update script is not an enabled hook. Configure and test a supported scan/webhook only if desired.",
            False,
        ),
    ),
    "rclone": (
        (
            "host_mount",
            "Confirm mount access and deployment",
            "Supply host FUSE/device permissions and verify the mount from each consumer's path namespace. DUMB cannot add Docker device/bind mappings to its own running container.",
            True,
        ),
    ),
    "postgres": (
        (
            "backups",
            "Choose database backup policy",
            "Back up the PostgreSQL databases and test restore. Selecting PostgreSQL for an established app requires its separate guarded migration workflow.",
            False,
        ),
    ),
}


def service_roles(key: str) -> list[dict]:
    return [
        {"key": role, "title": title}
        for role, (title, services) in ROLES.items()
        if key in services
    ]


def docs_url(key: str) -> str:
    if key == "plex":
        return "https://dumbarr.com/services/core/plex-media-server/"
    if key == "postgres":
        return "https://dumbarr.com/services/dependent/postgres/"
    if key == "traefik":
        return "https://dumbarr.com/features/embedded-ui/"
    group = (
        "core"
        if key in CORE_KEYS
        else "optional" if key in OPTIONAL_KEYS else "dependent"
    )
    return f"https://dumbarr.com/services/{group}/{key.replace('_', '-')}/"


def service_metadata(key: str) -> dict:
    return {
        "roles": service_roles(key),
        "setup_tasks": [
            {"key": task, "label": label, "instructions": text, "required": required}
            for task, label, text, required in MANUAL_TASKS.get(key, ())
        ],
    }
