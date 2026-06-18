from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import json
import os
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict
import requests

from api.routers.logs import find_log_file
from api.routers.process import _collect_process_entries, dependency_graph
from utils.config_loader import CONFIG_MANAGER
from utils.dependencies import get_api_state, get_logger, get_optional_current_user
from utils.logger import redact_sensitive_log_data

ai_router = APIRouter()

DEFAULT_AI_CONFIG = {
    "enabled": False,
    "provider": "ollama",
    "base_url": "http://127.0.0.1:11434",
    "model": "",
    "api_key": "",
    "timeout_sec": 60,
    "temperature": 0.2,
    "max_log_chars": 20000,
    "include_logs": True,
    "include_service_config": True,
    "include_dependency_graph": True,
    "include_docs_context": True,
    "include_process_list": False,
    "max_docs_chars": 12000,
}

SECRET_KEY_HINTS = (
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "authorization",
    "client_secret",
    "plex_token",
    "github_token",
    "tunnel_token",
)

DOCS_BASE_URL = "https://dumbarr.com"

DOCS_CONTEXT_INDEX = [
    {
        "path": "features/ai-assistant.md",
        "title": "AI Assistant",
        "keywords": [
            "ai",
            "assistant",
            "provider",
            "ollama",
            "openai",
            "litellm",
            "claude",
        ],
    },
    {
        "path": "frontend/service-pages.md",
        "title": "Frontend Service Pages",
        "keywords": ["service", "config", "logs", "embedded", "dependency", "restart"],
    },
    {
        "path": "architecture/backend.md",
        "title": "Backend Architecture",
        "keywords": ["stack", "startup", "orchestration", "backend", "api"],
    },
    {
        "path": "features/index.md",
        "title": "Features Overview",
        "keywords": [
            "stack",
            "overview",
            "features",
            "whole",
            "services",
            "usenet",
            "workflow",
            "use",
        ],
    },
    {
        "path": "getting-started/index.md",
        "title": "Getting Started",
        "keywords": ["getting started", "workflow", "usenet", "debrid", "both", "use"],
    },
    {
        "path": "frontend/onboarding.md",
        "title": "Guided Onboarding",
        "keywords": ["onboarding", "usenet", "workflow", "debrid", "both", "arrs"],
    },
    {
        "path": "reference/core-service.md",
        "title": "Core Service Routing",
        "keywords": [
            "core_service",
            "workflow",
            "usenet",
            "debrid",
            "nzbdav",
            "altmount",
            "decypharr",
            "arr",
        ],
    },
    {
        "path": "services/core/index.md",
        "title": "Core Services",
        "keywords": [
            "core",
            "services",
            "workflow",
            "usenet",
            "debrid",
            "nzbdav",
            "altmount",
            "decypharr",
        ],
    },
    {
        "path": "services/core/nzbdav.md",
        "title": "NzbDAV",
        "keywords": ["usenet", "nzbdav", "nzb", "webdav", "sabnzbd", "arr"],
    },
    {
        "path": "services/core/altmount.md",
        "title": "AltMount",
        "keywords": ["usenet", "altmount", "webdav", "sabnzbd", "arr"],
    },
    {
        "path": "services/core/decypharr.md",
        "title": "Decypharr",
        "keywords": [
            "usenet",
            "debrid",
            "decypharr",
            "hybrid",
            "sabnzbd",
            "arr",
        ],
    },
    {
        "path": "api/process.md",
        "title": "Process Management API",
        "keywords": ["process", "start", "stop", "restart", "dependency", "capability"],
    },
    {
        "path": "features/embedded-ui.md",
        "title": "Embedded UI",
        "keywords": ["embedded", "iframe", "proxy", "ui", "routing", "websocket"],
    },
    {
        "path": "features/auto-restart.md",
        "title": "Auto Restart",
        "keywords": ["restart", "crash", "loop", "unhealthy", "recovery"],
    },
    {
        "path": "features/auto-update.md",
        "title": "Auto Update",
        "keywords": ["update", "install", "version", "branch", "release"],
    },
    {
        "path": "features/symlinks.md",
        "title": "Symlink Operations",
        "keywords": ["symlink", "manifest", "repair", "restore", "migration"],
    },
    {
        "path": "features/seerr-sync.md",
        "title": "Seerr Sync",
        "keywords": ["seerr", "overseerr", "jellyseerr", "sync", "request"],
    },
    {
        "path": "architecture/traefik.md",
        "title": "Traefik Architecture",
        "keywords": ["traefik", "proxy", "router", "middleware", "tls"],
    },
    {
        "path": "services/dependent/postgres.md",
        "title": "PostgreSQL",
        "keywords": ["postgres", "postgresql", "database", "pgadmin", "5432"],
    },
    {
        "path": "services/dependent/rclone.md",
        "title": "rclone",
        "keywords": ["rclone", "mount", "webdav", "fuse", "debrid"],
    },
    {
        "path": "services/dependent/zurg.md",
        "title": "Zurg",
        "keywords": ["zurg", "realdebrid", "webdav"],
    },
    {
        "path": "services/optional/cloudflared.md",
        "title": "Cloudflared",
        "keywords": ["cloudflared", "cloudflare", "tunnel", "zero trust", "502"],
    },
    {
        "path": "services/optional/traefik-proxy-admin.md",
        "title": "Traefik Proxy Admin",
        "keywords": ["traefik_proxy_admin", "tpa", "auth", "sso", "domain"],
    },
]

SERVICE_DOC_PATHS = {
    "altmount": "services/core/altmount.md",
    "cli_battery": "services/dependent/cli-battery.md",
    "cli_debrid": "services/core/cli-debrid.md",
    "cloudflared": "services/optional/cloudflared.md",
    "decypharr": "services/core/decypharr.md",
    "dumb_api": "services/dumb/api.md",
    "dumb_frontend": "services/dumb/dumb-frontend.md",
    "emby": "services/core/emby.md",
    "jellyfin": "services/core/jellyfin.md",
    "lidarr": "services/core/lidarr.md",
    "neutarr": "services/core/neutarr.md",
    "nzbdav": "services/core/nzbdav.md",
    "pgadmin": "services/optional/pgadmin.md",
    "plex": "services/core/plex-media-server.md",
    "plex_debrid": "services/core/plex-debrid.md",
    "postgres": "services/dependent/postgres.md",
    "profilarr": "services/core/profilarr.md",
    "prowlarr": "services/core/prowlarr.md",
    "radarr": "services/core/radarr.md",
    "rclone": "services/dependent/rclone.md",
    "riven_backend": "services/core/riven-backend.md",
    "riven_frontend": "services/optional/riven-frontend.md",
    "seerr": "services/core/seerr.md",
    "sonarr": "services/core/sonarr.md",
    "tautulli": "services/optional/tautulli.md",
    "traefik_proxy_admin": "services/optional/traefik-proxy-admin.md",
    "whisparr": "services/core/whisparr.md",
    "zilean": "services/optional/zilean.md",
    "zurg": "services/dependent/zurg.md",
}

DUMB_SERVICE_CATALOG = {
    "usenet_workflows": [
        {
            "service": "NzbDAV",
            "config_key": "nzbdav",
            "role": "Usenet WebDAV gateway and Arr download-client integration.",
            "use_when": "You want a dedicated Usenet workflow with WebDAV-backed access and DUMB-managed Arr integration.",
            "docs": "https://dumbarr.com/services/core/nzbdav/",
        },
        {
            "service": "AltMount",
            "config_key": "altmount",
            "role": "Alternative Usenet WebDAV and SABnzbd-compatible workflow.",
            "use_when": "You want the AltMount Usenet workflow instead of, or alongside, NzbDAV.",
            "docs": "https://dumbarr.com/services/core/altmount/",
        },
        {
            "service": "Decypharr",
            "config_key": "decypharr",
            "role": "Debrid and native Usenet workflow service with Arr integrations.",
            "use_when": "You want a hybrid Debrid plus Usenet workflow, or Decypharr's native Usenet support.",
            "docs": "https://dumbarr.com/services/core/decypharr/",
        },
    ],
    "arr_apps": [
        {
            "service": "Sonarr",
            "role": "TV automation; set core_service to the selected workflow.",
        },
        {
            "service": "Radarr",
            "role": "Movie automation; set core_service to the selected workflow.",
        },
        {
            "service": "Lidarr",
            "role": "Music automation; set core_service to the selected workflow.",
        },
        {
            "service": "Whisparr",
            "role": "Adult-content automation; set core_service to the selected workflow.",
        },
    ],
    "indexers_and_support": [
        {
            "service": "Prowlarr",
            "role": "Indexer management and sync to Arr apps.",
        },
        {
            "service": "rclone",
            "role": "Mounts WebDAV/debrid-backed storage for workflows that need it.",
        },
    ],
    "guidance": [
        "For DUMB Usenet planning, prefer Decypharr, NzbDAV, and/or AltMount as workflow services.",
        "SABnzbd-compatible means a DUMB service can expose an Arr download-client API; do not recommend external SABnzbd/NZBGet as the primary DUMB service unless the user explicitly asks for external clients.",
        "Use core_service on Arr instances to wire Sonarr/Radarr/Lidarr/Whisparr to decypharr, nzbdav, altmount, or a list for combined workflows.",
    ],
}

DUMB_WORKFLOW_RULES = {
    "authority": "This is a DUMB-specific answer. DUMB docs and dumb_service_catalog override generic media-stack knowledge.",
    "usenet_primary_dumb_services": ["Decypharr", "NzbDAV", "AltMount"],
    "usenet_support_services": [
        "Sonarr",
        "Radarr",
        "Lidarr",
        "Whisparr",
        "Prowlarr",
        "rclone",
    ],
    "not_primary_dumb_services": ["SABnzbd", "NZBGet", "NZBHydra"],
    "rules": [
        "Recommend Decypharr, NzbDAV, and/or AltMount for DUMB Usenet workflow selection.",
        "Describe Arr apps as automation clients that should be wired to the selected DUMB workflow using core_service.",
        "Describe Prowlarr as indexer management for Arr apps.",
        "Describe rclone only when the selected workflow needs mount support.",
        "Do not recommend SABnzbd, NZBGet, or NZBHydra as primary DUMB services unless the operator explicitly asks for external/non-DUMB clients.",
    ],
}

DUMB_PRODUCT_FACTS = {
    "name": "DUMB",
    "expansion": "Debrid Unlimited Media Bridge",
    "description": (
        "An all-in-one Docker-oriented media automation stack for Debrid and Usenet "
        "workflows, Arr automation, media servers, request/watchlist tools, service "
        "management, logs, metrics, updates, and embedded service UIs."
    ),
    "do_not_expand_as": [
        "Docker Universal Media Box",
        "Docker Unified Media Box",
        "Download Unified Media Box",
    ],
}


class AiSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    provider: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    timeout_sec: Optional[int] = None
    temperature: Optional[float] = None
    max_log_chars: Optional[int] = None
    include_logs: Optional[bool] = None
    include_service_config: Optional[bool] = None
    include_dependency_graph: Optional[bool] = None
    include_docs_context: Optional[bool] = None
    include_process_list: Optional[bool] = None
    max_docs_chars: Optional[int] = None
    model_config = ConfigDict(extra="forbid")


class AiDiagnosticRequest(BaseModel):
    process_name: str
    question: Optional[str] = None
    include_logs: Optional[bool] = None
    include_service_config: Optional[bool] = None
    include_dependency_graph: Optional[bool] = None
    include_docs_context: Optional[bool] = None
    include_process_list: Optional[bool] = None
    max_log_chars: Optional[int] = None
    max_docs_chars: Optional[int] = None
    dry_run: Optional[bool] = False
    model_config = ConfigDict(extra="forbid")


class AiStackDiagnosticRequest(BaseModel):
    question: Optional[str] = None
    include_logs: Optional[bool] = None
    include_service_config: Optional[bool] = False
    include_dependency_graph: Optional[bool] = None
    include_docs_context: Optional[bool] = None
    include_process_list: Optional[bool] = True
    max_log_chars: Optional[int] = None
    max_docs_chars: Optional[int] = None
    max_services: Optional[int] = 60
    dry_run: Optional[bool] = False
    model_config = ConfigDict(extra="forbid")


class AiProviderRequest(AiSettingsUpdate):
    prompt: Optional[str] = None


def _find_service_config_with_path(
    config: dict, process_name: str, parent_path: str = ""
) -> tuple[dict | None, str | None]:
    for key, value in (config or {}).items():
        path = f"{parent_path}.{key}" if parent_path else str(key)
        if isinstance(value, dict) and value.get("process_name") == process_name:
            return value, path
        if isinstance(value, dict) and isinstance(value.get("instances"), dict):
            for instance_name, instance in value["instances"].items():
                instance_path = f"{path}.instances.{instance_name}"
                if (
                    isinstance(instance, dict)
                    and instance.get("process_name") == process_name
                ):
                    return instance, instance_path
        if isinstance(value, dict):
            found, found_path = _find_service_config_with_path(
                value, process_name, path
            )
            if found:
                return found, found_path
    return None, None


def _ai_config() -> dict:
    configured = CONFIG_MANAGER.config.get("dumb", {}).get("ai", {}) or {}
    merged = dict(DEFAULT_AI_CONFIG)
    if isinstance(configured, dict):
        merged.update(configured)
    return merged


def _public_settings(config: dict) -> dict:
    public = dict(config)
    api_key = str(public.pop("api_key", "") or "")
    public["api_key_configured"] = bool(api_key.strip())
    return public


def _is_secret_key(key: str) -> bool:
    normalized = str(key or "").lower().replace("-", "_")
    return any(hint in normalized for hint in SECRET_KEY_HINTS)


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, child in value.items():
            if _is_secret_key(str(key)):
                redacted[key] = "[REDACTED]" if child not in ("", None) else child
            else:
                redacted[key] = _redact_value(child)
        return redacted
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_log_data(value)
    return value


def _tail_log(path: Path, max_chars: int) -> str:
    max_chars = max(1000, min(int(max_chars or 20000), 200000))
    # UTF-8 chars can span bytes; read a little extra and trim decoded text.
    read_bytes = max_chars * 2
    size = path.stat().st_size
    with open(path, "rb") as log_file:
        log_file.seek(max(0, size - read_bytes))
        text = log_file.read().decode("utf-8", "replace")
    return redact_sensitive_log_data(text[-max_chars:])


def _docs_root_candidates() -> list[Path]:
    candidates = []
    configured_path = os.environ.get("DUMB_DOCS_PATH")
    if configured_path:
        candidates.append(Path(configured_path))
    repo_root = Path(__file__).resolve().parents[2]
    candidates.extend(
        [
            Path("/docs"),
            Path("/DUMB_docs/docs"),
            Path("/workspace/DUMB_docs/docs"),
            repo_root.parent / "DUMB_docs" / "docs",
            Path("/home/id10t/VSCode/DUMB_docs/docs"),
        ]
    )
    return candidates


def _find_docs_root() -> Path | None:
    for candidate in _docs_root_candidates():
        if candidate and candidate.exists() and (candidate / "index.md").exists():
            return candidate
    return None


def _docs_url(path: str) -> str:
    route = path[:-3] if path.endswith(".md") else path
    if route.endswith("/index"):
        route = route[: -len("/index")]
    return f"{DOCS_BASE_URL}/{route.strip('/')}/"


def _normalize_doc_text(text: str) -> str:
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*]\([^)]+\)", "", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _read_local_doc(docs_root: Path, path: str) -> str:
    doc_path = docs_root / path
    if not doc_path.exists():
        return ""
    try:
        return _normalize_doc_text(
            doc_path.read_text(encoding="utf-8", errors="replace")
        )
    except OSError:
        return ""


def _fetch_public_doc(path: str, timeout: int = 8) -> str:
    url = _docs_url(path)
    try:
        response = requests.get(
            url,
            headers={"accept": "text/html, text/plain;q=0.9"},
            timeout=timeout,
        )
    except requests.RequestException:
        return ""
    if response.status_code >= 400:
        return ""
    return _normalize_doc_text(response.text)


def _read_doc_context_source(docs_root: Path | None, path: str) -> tuple[str, str]:
    if docs_root:
        text = _read_local_doc(docs_root, path)
        if text:
            return text, "local"
    text = _fetch_public_doc(path)
    return (text, "web") if text else ("", "")


def _service_doc_path(config_key: str | None, process_name: str) -> str | None:
    key = str(config_key or "").lower()
    if key in SERVICE_DOC_PATHS:
        return SERVICE_DOC_PATHS[key]
    normalized_name = str(process_name or "").lower()
    for service_key, path in SERVICE_DOC_PATHS.items():
        if service_key.replace("_", " ") in normalized_name:
            return path
    return None


def _score_doc(entry: dict, haystack: str, service_doc_path: str | None) -> int:
    score = 0
    if entry["path"] == service_doc_path:
        score += 80
    for keyword in entry.get("keywords", []):
        if keyword.lower() in haystack:
            score += 10
    path_bits = entry["path"].replace("-", " ").replace("_", " ").replace("/", " ")
    for bit in path_bits.split():
        if len(bit) > 3 and bit.lower() in haystack:
            score += 2
    return score


def _build_docs_context(
    bundle: dict,
    ai_config: dict,
    request: AiDiagnosticRequest | AiStackDiagnosticRequest,
) -> dict:
    docs_root = _find_docs_root()
    max_chars = max(
        1000,
        min(
            int(request.max_docs_chars or ai_config.get("max_docs_chars") or 12000),
            60000,
        ),
    )

    haystack = " ".join(
        [
            str(bundle.get("process_name") or ""),
            str(bundle.get("scope") or ""),
            str(bundle.get("config_key") or ""),
            str(bundle.get("service_path") or ""),
            str(bundle.get("question") or ""),
            json.dumps(bundle.get("service_status") or {}, sort_keys=True),
            json.dumps(bundle.get("stack_summary") or {}, sort_keys=True)[:4000],
            json.dumps(bundle.get("processes") or [], sort_keys=True)[:4000],
            json.dumps(bundle.get("logs") or {}, sort_keys=True)[:4000],
        ]
    ).lower()
    service_doc_path = _service_doc_path(
        bundle.get("config_key"), bundle.get("process_name")
    )

    entries_by_path = {entry["path"]: dict(entry) for entry in DOCS_CONTEXT_INDEX}
    if service_doc_path:
        entries_by_path.setdefault(
            service_doc_path,
            {
                "path": service_doc_path,
                "title": service_doc_path.rsplit("/", 1)[-1].removesuffix(".md"),
                "keywords": [],
            },
        )

    scored = []
    for entry in entries_by_path.values():
        if docs_root and not (docs_root / entry["path"]).exists():
            continue
        score = _score_doc(entry, haystack, service_doc_path)
        if score > 0 or entry["path"] in {
            "frontend/service-pages.md",
            "api/process.md",
            "architecture/backend.md",
        }:
            scored.append((score, entry))

    scored.sort(key=lambda item: (-item[0], item[1]["path"]))

    sources = []
    remaining = max_chars
    for score, entry in scored[:6]:
        if remaining <= 0:
            break
        text, source = _read_doc_context_source(docs_root, entry["path"])
        if not text:
            continue
        excerpt = text[:remaining].strip()
        if not excerpt:
            continue
        sources.append(
            {
                "title": entry.get("title") or entry["path"],
                "path": entry["path"],
                "url": _docs_url(entry["path"]),
                "source": source,
                "score": score,
                "excerpt": excerpt,
            }
        )
        remaining -= len(excerpt)

    return {
        "available": bool(sources),
        "source": "local" if docs_root else "web",
        "docs_root": str(docs_root) if docs_root else "",
        "note": (
            ""
            if sources
            else "No matching DUMB docs context could be loaded from local files or dumbarr.com."
        ),
        "max_chars": max_chars,
        "sources": sources,
    }


def _build_diagnostic_bundle(
    request: AiDiagnosticRequest,
    ai_config: dict,
    api_state,
    logger,
    current_user: str,
) -> dict:
    process_name = str(request.process_name or "").strip()
    if not process_name:
        raise HTTPException(status_code=400, detail="process_name is required")

    service_config, service_path = _find_service_config_with_path(
        CONFIG_MANAGER.config, process_name
    )
    if not service_config:
        raise HTTPException(status_code=404, detail="Service not found")

    include_logs = (
        ai_config.get("include_logs", True)
        if request.include_logs is None
        else request.include_logs
    )
    include_service_config = (
        ai_config.get("include_service_config", True)
        if request.include_service_config is None
        else request.include_service_config
    )
    include_dependency_graph = (
        ai_config.get("include_dependency_graph", True)
        if request.include_dependency_graph is None
        else request.include_dependency_graph
    )
    include_docs_context = (
        ai_config.get("include_docs_context", True)
        if request.include_docs_context is None
        else request.include_docs_context
    )
    include_process_list = (
        ai_config.get("include_process_list", False)
        if request.include_process_list is None
        else request.include_process_list
    )
    max_log_chars = request.max_log_chars or ai_config.get("max_log_chars", 20000)

    key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
    bundle = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dumb_product": DUMB_PRODUCT_FACTS,
        "process_name": process_name,
        "config_key": key,
        "instance_name": instance_name,
        "service_path": service_path,
        "service_status": (
            api_state.get_status_details(process_name, include_health=True)
            if api_state
            else {}
        ),
        "question": request.question or "",
    }

    if include_service_config:
        bundle["service_config"] = _redact_value(service_config)

    if include_dependency_graph:
        try:
            bundle["dependency_graph"] = dependency_graph(
                process_name=process_name,
                scope="runtime",
                api_state=api_state,
                logger=logger,
                current_user=current_user,
            )
        except Exception as exc:
            logger.debug("AI diagnostic dependency graph unavailable: %s", exc)
            bundle["dependency_graph_error"] = "Dependency graph unavailable."

    if include_process_list:
        bundle["processes"] = [
            _redact_value(
                {
                    "name": entry.get("name"),
                    "process_name": entry.get("process_name"),
                    "config_key": entry.get("config_key"),
                    "enabled": entry.get("enabled"),
                    "status": (
                        api_state.get_status(entry.get("process_name"))
                        if api_state and entry.get("process_name")
                        else "unknown"
                    ),
                }
            )
            for entry in _collect_process_entries()
        ]

    if include_logs:
        log_path = find_log_file(process_name, logger)
        if log_path and log_path.exists():
            bundle["logs"] = {
                "path": str(log_path),
                "tail_chars": int(max_log_chars),
                "content": _tail_log(log_path, int(max_log_chars)),
            }
        else:
            bundle["logs"] = {"content": "", "note": "No log file found."}

    if include_docs_context:
        bundle["docs_context"] = _build_docs_context(bundle, ai_config, request)

    return bundle


def _compact_process_entry(entry: dict, api_state) -> dict:
    process_name = entry.get("process_name")
    status_details = (
        api_state.get_status_details(process_name, include_health=True)
        if api_state and process_name
        else {}
    )
    return _redact_value(
        {
            "name": entry.get("name"),
            "process_name": process_name,
            "config_key": entry.get("config_key"),
            "enabled": entry.get("enabled"),
            "version": entry.get("version"),
            "status": status_details.get("status")
            or (
                api_state.get_status(process_name)
                if api_state and process_name
                else "unknown"
            ),
            "healthy": status_details.get("healthy"),
            "health_reason": status_details.get("health_reason"),
        }
    )


def _summarize_stack_processes(processes: list[dict], api_state) -> dict:
    services = [_compact_process_entry(entry, api_state) for entry in processes]
    enabled = [service for service in services if service.get("enabled") is True]
    unhealthy = [service for service in enabled if service.get("healthy") is False]
    stopped = [
        service
        for service in enabled
        if str(service.get("status") or "").lower() == "stopped"
    ]
    unknown = [
        service
        for service in enabled
        if str(service.get("status") or "").lower() in {"", "unknown", "none"}
    ]
    running = [
        service
        for service in enabled
        if str(service.get("status") or "").lower() == "running"
    ]
    return {
        "counts": {
            "total": len(services),
            "enabled": len(enabled),
            "running": len(running),
            "stopped": len(stopped),
            "unhealthy": len(unhealthy),
            "unknown": len(unknown),
        },
        "attention": {
            "unhealthy": unhealthy,
            "stopped": stopped,
            "unknown": unknown,
        },
    }


def _build_stack_dependency_graph(
    processes: list[dict],
    api_state,
    logger,
    current_user: str,
    max_services: int,
) -> dict:
    nodes_by_id = {}
    edges_by_key = {}
    errors = []
    enabled_processes = [
        entry
        for entry in processes
        if entry.get("enabled") is True and entry.get("process_name")
    ][:max_services]

    for entry in enabled_processes:
        process_name = entry.get("process_name")
        try:
            graph = dependency_graph(
                process_name=process_name,
                scope="runtime",
                api_state=api_state,
                logger=logger,
                current_user=current_user,
            )
        except Exception as exc:
            errors.append({"process_name": process_name, "error": str(exc)})
            continue
        for node in graph.get("nodes") or []:
            node_id = node.get("id") or node.get("process_name")
            if node_id:
                nodes_by_id[node_id] = node
        for edge in graph.get("edges") or []:
            edge_key = (
                edge.get("source"),
                edge.get("target"),
                edge.get("strength"),
                tuple(edge.get("signals") or []),
            )
            edges_by_key[edge_key] = edge

    return {
        "scope": "runtime",
        "partial": bool(errors),
        "nodes": list(nodes_by_id.values()),
        "edges": list(edges_by_key.values()),
        "errors": errors[:10],
    }


def _stack_log_targets(stack_summary: dict) -> list[str]:
    targets = []
    for group in ("unhealthy", "stopped", "unknown"):
        for service in stack_summary.get("attention", {}).get(group, []) or []:
            process_name = service.get("process_name")
            if process_name and process_name not in targets:
                targets.append(process_name)
    return targets[:12]


def _is_workflow_planning_question(question: str) -> bool:
    text = str(question or "").lower()
    planning_terms = (
        "what services",
        "which services",
        "what should i use",
        "which should i use",
        "recommend",
        "setup",
        "set up",
        "workflow",
        "usenet",
        "debrid",
    )
    return any(term in text for term in planning_terms)


def _is_usenet_question(question: str) -> bool:
    return "usenet" in str(question or "").lower()


def _is_product_identity_question(question: str) -> bool:
    text = str(question or "").lower()
    if "dumb" not in text:
        return False
    identity_terms = (
        "stand for",
        "stands for",
        "what is dumb",
        "what's dumb",
        "define dumb",
        "acronym",
        "expansion",
        "full name",
        "meaning",
    )
    return any(term in text for term in identity_terms)


def _authoritative_product_identity_answer() -> str:
    product = DUMB_PRODUCT_FACTS
    return "\n".join(
        [
            "## Direct Answer",
            "",
            f"DUMB stands for **{product['expansion']}**.",
            "",
            "## Why",
            "",
            "This is a fixed DUMB product fact included in the diagnostic bundle. Do not use other acronym expansions for DUMB.",
        ]
    )


def _process_lookup(bundle: dict) -> dict[str, dict]:
    lookup = {}
    for process in bundle.get("processes") or []:
        if not isinstance(process, dict):
            continue
        names = {
            str(process.get("name") or "").lower(),
            str(process.get("process_name") or "").lower(),
            str(process.get("config_key") or "").lower(),
        }
        for name in names:
            if name:
                lookup[name] = process
    return lookup


def _service_state(processes: dict[str, dict], *keys: str) -> str:
    for key in keys:
        process = processes.get(key.lower())
        if process:
            status = process.get("status") or process.get("state") or "configured"
            healthy = process.get("healthy")
            if healthy is False:
                return f"{status}, unhealthy"
            return str(status)
    return "not currently enabled/listed"


def _authoritative_usenet_answer(bundle: dict) -> str:
    processes = _process_lookup(bundle)
    decypharr_state = _service_state(processes, "decypharr")
    nzbdav_state = _service_state(processes, "nzbdav")
    altmount_state = _service_state(processes, "altmount")
    prowlarr_state = _service_state(processes, "prowlarr", "Prowlarr")
    rclone_state = _service_state(processes, "rclone")

    return "\n".join(
        [
            "## Direct Answer",
            "",
            "For Usenet inside DUMB, choose one of the DUMB-native workflow services: **Decypharr**, **NzbDAV**, or **AltMount**. Do not add SABnzbd, NZBGet, or NZBHydra as the primary recommendation unless you intentionally want an external/non-DUMB download-client workflow.",
            "",
            "## Recommended DUMB Path",
            "",
            "1. **Decypharr**: best fit if you want one workflow that can cover debrid plus native Usenet-style Arr integration.",
            "2. **NzbDAV**: best fit if you want a dedicated Usenet WebDAV/SABnzbd-compatible workflow managed by DUMB.",
            "3. **AltMount**: best fit if you prefer the AltMount Usenet workflow or want to compare it alongside NzbDAV.",
            "",
            "## Support Services",
            "",
            "- **Prowlarr**: use it for indexer management and syncing indexers to Sonarr/Radarr/Lidarr/Whisparr.",
            "- **Sonarr/Radarr/Lidarr/Whisparr**: keep these as automation clients, then wire their `core_service` to the selected DUMB workflow service.",
            "- **rclone**: use it only when the selected workflow needs mount support.",
            "- **PostgreSQL/pgAdmin**: support database/admin services; they are not the Usenet workflow itself.",
            "",
            "## Current Stack Signal",
            "",
            f"- Decypharr: {decypharr_state}",
            f"- NzbDAV: {nzbdav_state}",
            f"- AltMount: {altmount_state}",
            f"- Prowlarr: {prowlarr_state}",
            f"- rclone: {rclone_state}",
            "",
            "## Next Steps",
            "",
            "1. Pick **Decypharr**, **NzbDAV**, or **AltMount** as the Usenet workflow service.",
            "2. Start or enable **Prowlarr** if you want indexer management.",
            "3. Point Arr apps at the chosen workflow with `core_service`.",
            "4. Use the selected service page's AI Assist for service-specific setup or log troubleshooting.",
        ]
    )


def _looks_like_external_usenet_recommendation(analysis: str) -> bool:
    text = str(analysis or "").lower()
    forbidden = ("sabnzbd", "nzbget", "nzbhydra")
    primary_language = (
        "recommend",
        "choose",
        "install",
        "add one",
        "primary",
        "download client",
        "set up",
    )
    return any(term in text for term in forbidden) and any(
        term in text for term in primary_language
    )


def _finalize_stack_analysis(bundle: dict, analysis: str) -> str:
    question = bundle.get("question") or ""
    if _is_product_identity_question(question):
        return _authoritative_product_identity_answer()
    if not (_is_workflow_planning_question(question) and _is_usenet_question(question)):
        return analysis
    authoritative = _authoritative_usenet_answer(bundle)
    if _looks_like_external_usenet_recommendation(analysis):
        return authoritative
    if not analysis:
        return authoritative
    return f"{authoritative}\n\n## Provider Notes\n\n{analysis}"


def _build_stack_diagnostic_bundle(
    request: AiStackDiagnosticRequest,
    ai_config: dict,
    api_state,
    logger,
    current_user: str,
) -> dict:
    max_services = max(5, min(int(request.max_services or 60), 120))
    include_logs = (
        ai_config.get("include_logs", True)
        if request.include_logs is None
        else request.include_logs
    )
    include_service_config = request.include_service_config is True
    include_dependency_graph = (
        ai_config.get("include_dependency_graph", True)
        if request.include_dependency_graph is None
        else request.include_dependency_graph
    )
    include_docs_context = (
        ai_config.get("include_docs_context", True)
        if request.include_docs_context is None
        else request.include_docs_context
    )
    include_process_list = request.include_process_list is not False
    max_log_chars = max(
        500,
        min(int(request.max_log_chars or ai_config.get("max_log_chars", 20000)), 50000),
    )

    processes = _collect_process_entries()[:max_services]
    stack_summary = _summarize_stack_processes(processes, api_state)
    bundle = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dumb_product": DUMB_PRODUCT_FACTS,
        "scope": "stack",
        "question": request.question or "",
        "stack_summary": stack_summary,
        "dumb_service_catalog": DUMB_SERVICE_CATALOG,
        "dumb_workflow_rules": DUMB_WORKFLOW_RULES,
    }

    if include_process_list:
        bundle["processes"] = [
            _compact_process_entry(entry, api_state) for entry in processes
        ]

    if include_dependency_graph:
        bundle["dependency_graph"] = _build_stack_dependency_graph(
            processes, api_state, logger, current_user, max_services
        )

    if include_service_config:
        configs = {}
        for entry in processes:
            process_name = entry.get("process_name")
            if process_name and entry.get("enabled") is True:
                configs[process_name] = _redact_value(entry.get("config") or {})
        bundle["service_configs"] = configs

    if include_logs:
        logs = {}
        per_service_chars = max(500, min(int(max_log_chars / 4), 8000))
        for process_name in _stack_log_targets(stack_summary):
            log_path = find_log_file(process_name, logger)
            if log_path and log_path.exists():
                logs[process_name] = {
                    "path": str(log_path),
                    "tail_chars": per_service_chars,
                    "content": _tail_log(log_path, per_service_chars),
                }
            else:
                logs[process_name] = {"content": "", "note": "No log file found."}
        bundle["logs"] = logs

    if include_docs_context:
        bundle["docs_context"] = _build_docs_context(bundle, ai_config, request)

    return bundle


def _truncate_text(value: Any, max_chars: int) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}\n[TRUNCATED {len(text) - max_chars} chars]"


def _compact_attention(stack_summary: dict, limit: int = 5) -> dict:
    attention = stack_summary.get("attention") or {}
    return {
        group: (attention.get(group) or [])[:limit]
        for group in ("unhealthy", "stopped", "unknown")
    }


def _compact_dependency_graph(
    graph: dict, max_nodes: int = 40, max_edges: int = 60
) -> dict:
    if not isinstance(graph, dict):
        return {}
    nodes = []
    for node in (graph.get("nodes") or [])[:max_nodes]:
        if not isinstance(node, dict):
            continue
        nodes.append(
            {
                "id": node.get("id") or node.get("process_name"),
                "label": node.get("label"),
                "state": node.get("state"),
                "key": node.get("key"),
            }
        )
    edges = []
    for edge in (graph.get("edges") or [])[:max_edges]:
        if not isinstance(edge, dict):
            continue
        edges.append(
            {
                "source": edge.get("source"),
                "target": edge.get("target"),
                "strength": edge.get("strength"),
                "signals": edge.get("signals"),
            }
        )
    return {
        "scope": graph.get("scope"),
        "partial": graph.get("partial"),
        "nodes": nodes,
        "edges": edges,
        "omitted": {
            "nodes": max(0, len(graph.get("nodes") or []) - len(nodes)),
            "edges": max(0, len(graph.get("edges") or []) - len(edges)),
        },
        "errors": (graph.get("errors") or [])[:5],
    }


def _compact_docs_context(context: dict, max_chars: int = 3500) -> dict:
    if not isinstance(context, dict):
        return {}
    sources = []
    remaining = max_chars
    for source in context.get("sources") or []:
        if remaining <= 0 or not isinstance(source, dict):
            break
        excerpt = _truncate_text(source.get("excerpt") or "", min(900, remaining))
        if not excerpt:
            continue
        sources.append(
            {
                "title": source.get("title"),
                "path": source.get("path"),
                "url": source.get("url"),
                "source": source.get("source"),
                "excerpt": excerpt,
            }
        )
        remaining -= len(excerpt)
    return {
        "available": bool(sources),
        "source": context.get("source"),
        "note": context.get("note"),
        "sources": sources,
    }


def _compact_logs(logs: dict, max_chars: int = 1800) -> dict:
    compact = {}
    remaining = max_chars
    for process_name, payload in (logs or {}).items():
        if remaining <= 0:
            break
        if not isinstance(payload, dict):
            continue
        content = _truncate_text(payload.get("content") or "", min(900, remaining))
        compact[process_name] = {
            "note": payload.get("note"),
            "content": content,
        }
        remaining -= len(content)
    return compact


def _compact_stack_bundle_for_provider(bundle: dict) -> dict:
    stack_summary = bundle.get("stack_summary") or {}
    planning_question = _is_workflow_planning_question(bundle.get("question") or "")
    compact = {
        "generated_at": bundle.get("generated_at"),
        "dumb_product": bundle.get("dumb_product") or DUMB_PRODUCT_FACTS,
        "scope": "stack",
        "question": bundle.get("question"),
        "analysis_mode": "workflow_planning" if planning_question else "diagnostic",
        "stack_summary": {
            "counts": stack_summary.get("counts") or {},
            "attention": _compact_attention(stack_summary),
        },
    }
    if bundle.get("dumb_service_catalog"):
        compact["dumb_service_catalog"] = bundle.get("dumb_service_catalog")
    if bundle.get("dumb_workflow_rules"):
        compact["dumb_workflow_rules"] = bundle.get("dumb_workflow_rules")
    if bundle.get("processes"):
        compact["processes"] = (bundle.get("processes") or [])[:30]
    if bundle.get("dependency_graph"):
        compact["dependency_graph"] = _compact_dependency_graph(
            bundle.get("dependency_graph") or {}
        )
    if bundle.get("logs") and not planning_question:
        compact["logs"] = _compact_logs(bundle.get("logs") or {})
    elif bundle.get("logs") and planning_question:
        compact["logs_note"] = (
            "Log tails were omitted from the provider prompt because this is a "
            "workflow-planning question; use service AI for log-specific diagnosis."
        )
    if bundle.get("service_configs"):
        compact["service_configs_note"] = (
            "Full service configs were omitted from the provider prompt to fit local model context. "
            "Use service-scoped AI for detailed config analysis."
        )
    if bundle.get("docs_context"):
        compact["docs_context"] = _compact_docs_context(
            bundle.get("docs_context") or {}
        )
    return compact


def _bundle_for_provider(bundle: dict) -> dict:
    if bundle.get("scope") == "stack":
        return _compact_stack_bundle_for_provider(bundle)
    return bundle


def _diagnostic_messages(bundle: dict) -> list[dict]:
    product_facts = bundle.get("dumb_product") or DUMB_PRODUCT_FACTS
    workflow_rules = bundle.get("dumb_workflow_rules") or {}
    product_block = (
        "DUMB PRODUCT FACTS:\n"
        + json.dumps(product_facts, indent=2, sort_keys=True)
        + "\n\n"
        "Use this expansion exactly; do not invent another expansion for DUMB.\n\n"
    )
    rule_block = ""
    if workflow_rules:
        rule_block = (
            "CRITICAL DUMB WORKFLOW RULES:\n"
            + json.dumps(workflow_rules, indent=2, sort_keys=True)
            + "\n\n"
            "If these rules conflict with generic model knowledge, follow these rules.\n\n"
        )

    system = (
        "You are the DUMB operator assistant. Diagnose service, startup, proxy, "
        "configuration, workflow, and dependency questions from the provided redacted "
        "runtime bundle. DUMB stands for Debrid Unlimited Media Bridge. "
        "Use docs_context when present as the project documentation "
        "source of truth, and treat dumb_service_catalog as authoritative for DUMB "
        "workflow planning. Answer the user's question directly before summarizing data. "
        "If the question asks what to use, compare the relevant DUMB services and give "
        "a recommended path. For Usenet planning, compare Decypharr, NzbDAV, AltMount, "
        "and the Arr/Prowlarr/rclone support roles. Do not recommend external SABnzbd "
        "or NZBGet as primary DUMB services unless the user explicitly asks for external "
        "clients; if you mention them, label them as external/non-DUMB. Do not merely "
        "describe the JSON structure. Cite concrete "
        "evidence from logs/status/config/docs, separate likely causes from guesses, "
        "and suggest safe next actions. Do not invent configuration values or claim "
        "changes were applied."
    )
    user = (
        "Answer the operator question using this DUMB diagnostic bundle. Prefer these "
        "sections when they fit: Direct Answer, Why, Relevant Evidence, Recommended "
        "Next Steps, and Risk Notes. For planning/workflow questions, use Options and "
        "Recommendation instead of Likely Cause.\n\n"
        + product_block
        + rule_block
        + json.dumps(bundle, indent=2, sort_keys=True)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _provider_error_detail(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("detail", "error", "message", "text"):
            value = data.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, dict):
                nested = _provider_error_detail(value)
                if nested:
                    return nested
            return str(value)
        return json.dumps(data, sort_keys=True)[:1000]
    if isinstance(data, list):
        return json.dumps(data)[:1000]
    return str(data or "Unknown provider error")


def _infer_open_webui_model_source(model: dict) -> tuple[str, str]:
    name = str(model.get("id") or model.get("name") or model.get("model") or "").lower()
    owned_by = str(model.get("owned_by") or model.get("ownedBy") or "").lower()
    provider = str(
        model.get("provider")
        or model.get("connection_type")
        or model.get("connection")
        or model.get("backend")
        or ""
    ).lower()
    info = model.get("info") if isinstance(model.get("info"), dict) else {}
    info_owned_by = str(info.get("owned_by") or info.get("ownedBy") or "").lower()
    meta = info.get("meta") if isinstance(info.get("meta"), dict) else {}
    meta_source = str(
        meta.get("source") or meta.get("provider") or meta.get("base_model_id") or ""
    ).lower()
    metadata_haystack = " ".join(
        value for value in (owned_by, provider, info_owned_by, meta_source) if value
    )

    local_markers = (
        "ollama",
        "local",
        "llama",
        "qwen",
        "deepseek",
        "gemma",
        "mistral",
        "mixtral",
        "phi",
        "starcoder",
        "codellama",
        "yi:",
        "granite",
    )
    external_markers = (
        "openai",
        "anthropic",
        "claude",
        "gpt-",
        "gpt4",
        "gemini",
        "google",
        "mistral-large",
        "cohere",
        "command-",
        "perplexity",
        "xai",
        "grok",
    )

    local_name_markers = (
        "local",
        "ollama",
        "llama",
        "qwen",
        "deepseek",
        "gemma",
        "phi",
        "starcoder",
        "codellama",
        "granite",
    )
    external_name_markers = (
        "openai",
        "gpt-",
        "gpt4",
        "claude",
        "anthropic",
        "gemini",
        "google",
        "cohere",
        "command-",
        "perplexity",
        "grok",
    )

    if any(marker in name for marker in local_name_markers):
        return "local", provider or info_owned_by or meta_source or "local"
    if any(marker in name for marker in external_name_markers):
        return "external", owned_by or provider or info_owned_by or "cloud"

    if any(marker in metadata_haystack for marker in external_markers):
        return "external", owned_by or provider or info_owned_by or "cloud"
    if any(marker in metadata_haystack for marker in local_markers):
        return "local", owned_by or provider or info_owned_by or "local"
    return "unknown", owned_by or provider or info_owned_by or ""


def _post_json(url: str, headers: dict, payload: dict, timeout: int) -> dict:
    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    try:
        data = response.json()
    except ValueError:
        data = {"text": response.text}
    if response.status_code >= 400:
        detail = _provider_error_detail(data)
        raise HTTPException(
            status_code=502,
            detail=f"AI provider request failed ({response.status_code}): {detail}",
        )
    return data


def _get_json(url: str, headers: dict, timeout: int) -> dict:
    response = requests.get(url, headers=headers, timeout=timeout)
    try:
        data = response.json()
    except ValueError:
        data = {"text": response.text}
    if response.status_code >= 400:
        detail = _provider_error_detail(data)
        raise HTTPException(
            status_code=502,
            detail=f"AI provider request failed ({response.status_code}): {detail}",
        )
    return data


def _effective_ai_config(request: AiProviderRequest | None = None) -> dict:
    config = _ai_config()
    if request is None:
        return config
    updates = request.model_dump(exclude_unset=True)
    updates.pop("prompt", None)
    existing_key = str(config.get("api_key") or "").strip()
    if updates.get("api_key") in (None, "") and existing_key:
        updates.pop("api_key", None)
    config.update(updates)
    return config


def _normalize_usage(data: dict, provider: str) -> dict:
    if not isinstance(data, dict):
        return {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    if usage:
        prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        total_tokens = usage.get("total_tokens")
        if total_tokens is None and (
            prompt_tokens is not None or completion_tokens is not None
        ):
            total_tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)
        normalized = {
            "provider": provider,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
        return {key: value for key, value in normalized.items() if value is not None}

    prompt_tokens = data.get("prompt_eval_count")
    completion_tokens = data.get("eval_count")
    total_tokens = data.get("total_tokens")
    if total_tokens is None and (
        prompt_tokens is not None or completion_tokens is not None
    ):
        total_tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)
    normalized = {
        "provider": provider,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "prompt_eval_count": prompt_tokens,
        "eval_count": completion_tokens,
        "total_duration": data.get("total_duration"),
        "load_duration": data.get("load_duration"),
        "prompt_eval_duration": data.get("prompt_eval_duration"),
        "eval_duration": data.get("eval_duration"),
    }
    return {key: value for key, value in normalized.items() if value is not None}


def _call_ai_messages_result(
    ai_config: dict, messages: list[dict], max_tokens: int = 1600
) -> dict:
    provider = str(ai_config.get("provider") or "ollama").lower()
    model = str(ai_config.get("model") or "").strip()
    timeout = int(ai_config.get("timeout_sec") or 60)
    temperature = float(ai_config.get("temperature", 0.2))
    api_key = str(ai_config.get("api_key") or "").strip()
    base_url = str(ai_config.get("base_url") or "").rstrip("/")

    local_default = DEFAULT_AI_CONFIG["base_url"].rstrip("/")

    if provider in {"anthropic", "claude"}:
        if not api_key:
            raise HTTPException(
                status_code=400, detail="Anthropic API key is not configured"
            )
        url = (
            base_url
            if base_url and base_url != local_default
            else "https://api.anthropic.com/v1/messages"
        )
        payload = {
            "model": model or "claude-3-5-sonnet-latest",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": messages[0]["content"],
            "messages": [messages[1]],
        }
        data = _post_json(
            url,
            {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            payload,
            timeout,
        )
        parts = data.get("content") or []
        content = "\n".join(
            part.get("text", "") for part in parts if isinstance(part, dict)
        ).strip()
        return {"content": content, "usage": _normalize_usage(data, provider)}

    if provider in {
        "openai",
        "openai_compatible",
        "compatible",
        "litellm",
        "open_webui",
    }:
        if provider == "openai" and not api_key:
            raise HTTPException(
                status_code=400, detail="OpenAI API key is not configured"
            )
        default_openai_url = "https://api.openai.com/v1" if provider == "openai" else ""
        url = base_url if base_url and base_url != local_default else default_openai_url
        if not url:
            raise HTTPException(
                status_code=400, detail="OpenAI-compatible base URL is not configured"
            )
        if provider == "open_webui" and not url.endswith("/api"):
            url = f"{url}/api"
        if not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model or "gpt-4.1-mini",
            "messages": messages,
            "temperature": temperature,
        }
        data = _post_json(url, headers, payload, timeout)
        choices = data.get("choices") or []
        if choices:
            content = str(choices[0].get("message", {}).get("content", "")).strip()
        else:
            content = str(data.get("text", "")).strip()
        return {"content": content, "usage": _normalize_usage(data, provider)}

    # Ollama/local default.
    url = base_url or "http://127.0.0.1:11434"
    if url.endswith("/v1"):
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        data = _post_json(
            f"{url}/chat/completions",
            headers,
            {"model": model, "messages": messages, "temperature": temperature},
            timeout,
        )
        choices = data.get("choices") or []
        content = (
            str(choices[0].get("message", {}).get("content", "")).strip()
            if choices
            else ""
        )
        return {"content": content, "usage": _normalize_usage(data, provider)}

    data = _post_json(
        f"{url}/api/chat",
        {"content-type": "application/json"},
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        },
        timeout,
    )
    message = data.get("message") if isinstance(data, dict) else {}
    content = str((message or {}).get("content") or data.get("response") or "").strip()
    return {"content": content, "usage": _normalize_usage(data, provider)}


def _call_ai_messages(
    ai_config: dict, messages: list[dict], max_tokens: int = 1600
) -> str:
    return _call_ai_messages_result(ai_config, messages, max_tokens)["content"]


def _call_ai_provider(ai_config: dict, bundle: dict) -> dict:
    return _call_ai_messages_result(
        ai_config, _diagnostic_messages(_bundle_for_provider(bundle))
    )


def _provider_test(ai_config: dict, prompt: str | None = None) -> dict:
    if not str(ai_config.get("model") or "").strip():
        raise HTTPException(status_code=400, detail="AI model is not configured")
    messages = [
        {
            "role": "system",
            "content": "You are testing a DUMB AI provider connection. Keep the reply short.",
        },
        {
            "role": "user",
            "content": prompt
            or "Reply with one short sentence confirming the DUMB AI provider test works.",
        },
    ]
    result = _call_ai_messages_result(ai_config, messages, max_tokens=120)
    return {
        "ok": True,
        "provider": ai_config.get("provider"),
        "model": ai_config.get("model"),
        "response": result.get("content", ""),
        "usage": result.get("usage") or {},
    }


def _list_provider_models(ai_config: dict) -> dict:
    provider = str(ai_config.get("provider") or "ollama").lower()
    timeout = int(ai_config.get("timeout_sec") or 60)
    base_url = str(ai_config.get("base_url") or "").rstrip("/")
    api_key = str(ai_config.get("api_key") or "").strip()
    local_default = DEFAULT_AI_CONFIG["base_url"].rstrip("/")

    if provider == "ollama":
        url = base_url or local_default
        data = _get_json(
            f"{url}/api/tags", {"content-type": "application/json"}, timeout
        )
        models = []
        for model in data.get("models") or []:
            if not isinstance(model, dict):
                continue
            name = str(model.get("name") or model.get("model") or "").strip()
            if name:
                models.append(
                    {
                        "name": name,
                        "size": model.get("size"),
                        "modified_at": model.get("modified_at"),
                    }
                )
        return {"provider": provider, "models": models}

    if provider in {
        "openai",
        "openai_compatible",
        "compatible",
        "litellm",
        "open_webui",
    }:
        default_openai_url = "https://api.openai.com/v1" if provider == "openai" else ""
        url = base_url if base_url and base_url != local_default else default_openai_url
        if not url:
            raise HTTPException(
                status_code=400, detail="OpenAI-compatible base URL is not configured"
            )
        if provider == "open_webui" and not url.endswith("/api"):
            url = f"{url}/api"
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        data = _get_json(f"{url}/models", headers, timeout)
        models = []
        for model in data.get("data") or []:
            if not isinstance(model, dict):
                continue
            name = str(model.get("id") or "").strip()
            if name:
                entry = {"name": name, "owned_by": model.get("owned_by")}
                if provider in {"open_webui", "litellm"}:
                    source, source_detail = _infer_open_webui_model_source(model)
                    entry["source"] = source
                    if source_detail:
                        entry["source_detail"] = source_detail
                models.append(entry)
        return {"provider": provider, "models": models}

    raise HTTPException(
        status_code=400,
        detail="Model discovery is supported for Ollama, Open WebUI, LiteLLM, and OpenAI-compatible providers only",
    )


@ai_router.get("/settings")
def get_ai_settings(current_user: str = Depends(get_optional_current_user)):
    return _public_settings(_ai_config())


@ai_router.put("/settings")
def update_ai_settings(
    request: AiSettingsUpdate,
    current_user: str = Depends(get_optional_current_user),
):
    updates = request.model_dump(exclude_unset=True)
    current = _ai_config()
    current.update(updates)
    if current.get("timeout_sec") is not None:
        current["timeout_sec"] = max(5, min(int(current["timeout_sec"]), 300))
    if current.get("max_log_chars") is not None:
        current["max_log_chars"] = max(1000, min(int(current["max_log_chars"]), 200000))
    if current.get("max_docs_chars") is not None:
        current["max_docs_chars"] = max(
            1000, min(int(current["max_docs_chars"]), 60000)
        )
    if current.get("temperature") is not None:
        current["temperature"] = max(0.0, min(float(current["temperature"]), 2.0))

    CONFIG_MANAGER.config.setdefault("dumb", {})["ai"] = current
    CONFIG_MANAGER.save_config()
    return _public_settings(current)


@ai_router.post("/test")
async def test_ai_provider(
    request: AiProviderRequest,
    current_user: str = Depends(get_optional_current_user),
):
    ai_config = _effective_ai_config(request)
    return await run_in_threadpool(_provider_test, ai_config, request.prompt)


@ai_router.post("/models")
async def list_ai_models(
    request: AiProviderRequest,
    current_user: str = Depends(get_optional_current_user),
):
    ai_config = _effective_ai_config(request)
    return await run_in_threadpool(_list_provider_models, ai_config)


@ai_router.post("/diagnose")
async def diagnose_service(
    request: AiDiagnosticRequest,
    api_state=Depends(get_api_state),
    logger=Depends(get_logger),
    current_user: str = Depends(get_optional_current_user),
):
    ai_config = _ai_config()
    bundle = await run_in_threadpool(
        _build_diagnostic_bundle,
        request,
        ai_config,
        api_state,
        logger,
        current_user,
    )
    if request.dry_run or not ai_config.get("enabled"):
        return {
            "enabled": bool(ai_config.get("enabled")),
            "provider": ai_config.get("provider"),
            "model": ai_config.get("model"),
            "analysis": "",
            "bundle": bundle,
            "usage": {},
            "dry_run": True,
        }

    if not str(ai_config.get("model") or "").strip():
        raise HTTPException(status_code=400, detail="AI model is not configured")

    provider_result = await run_in_threadpool(_call_ai_provider, ai_config, bundle)
    analysis = provider_result.get("content", "")
    return {
        "enabled": True,
        "provider": ai_config.get("provider"),
        "model": ai_config.get("model"),
        "analysis": analysis,
        "bundle": bundle,
        "usage": provider_result.get("usage") or {},
        "dry_run": False,
    }


@ai_router.post("/diagnose-stack")
async def diagnose_stack(
    request: AiStackDiagnosticRequest,
    api_state=Depends(get_api_state),
    logger=Depends(get_logger),
    current_user: str = Depends(get_optional_current_user),
):
    ai_config = _ai_config()
    bundle = await run_in_threadpool(
        _build_stack_diagnostic_bundle,
        request,
        ai_config,
        api_state,
        logger,
        current_user,
    )
    if request.dry_run or not ai_config.get("enabled"):
        return {
            "enabled": bool(ai_config.get("enabled")),
            "provider": ai_config.get("provider"),
            "model": ai_config.get("model"),
            "analysis": "",
            "bundle": bundle,
            "usage": {},
            "dry_run": True,
        }

    if not str(ai_config.get("model") or "").strip():
        raise HTTPException(status_code=400, detail="AI model is not configured")

    provider_result = await run_in_threadpool(_call_ai_provider, ai_config, bundle)
    analysis = provider_result.get("content", "")
    analysis = _finalize_stack_analysis(bundle, analysis)
    return {
        "enabled": True,
        "provider": ai_config.get("provider"),
        "model": ai_config.get("model"),
        "analysis": analysis,
        "bundle": bundle,
        "usage": provider_result.get("usage") or {},
        "dry_run": False,
    }
