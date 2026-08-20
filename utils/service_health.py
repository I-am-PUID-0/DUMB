"""Bounded application-level health probes for DUMB-managed services.

These probes intentionally test only local, read-only readiness endpoints. They
do not inspect application content health or remote integrations because those
signals should not cause DUMB to restart an otherwise operational service.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

MAX_RESPONSE_BYTES = 64 * 1024
MAX_COMPONENTS = 12

HEALTHY_STATES = {
    "available",
    "healthy",
    "ok",
    "operational",
    "pass",
    "passing",
    "ping",
    "pong",
    "ready",
    "running",
    "success",
    "up",
}
STARTING_STATES = {
    "initialising",
    "initializing",
    "migration",
    "migrating",
    "pending",
    "starting",
    "warming",
    "warming_up",
}
DEGRADED_STATES = {
    "degraded",
    "partial",
    "unknown",
    "warn",
    "warning",
}
UNHEALTHY_STATES = {
    "critical",
    "down",
    "error",
    "fail",
    "failed",
    "failure",
    "not_ready",
    "stopped",
    "unavailable",
    "unhealthy",
}

SERVARR_KEYS = {"lidarr", "prowlarr", "radarr", "sonarr", "whisparr"}


@dataclass(frozen=True)
class HttpProbe:
    name: str
    path: str
    port_field: str = "port"
    method: str = "GET"
    required_json_key: str | None = None
    required_text: str | None = None


HTTP_PROBES = {
    "infinidysk": HttpProbe("InfiniDysk backend health", "/health", "backend_port"),
    "jellyfin": HttpProbe("Jellyfin health", "/health"),
    "emby": HttpProbe("Emby application ping", "/emby/System/Ping"),
    "plex": HttpProbe(
        "Plex identity",
        "/identity",
        required_text="<MediaContainer",
    ),
    "seerr": HttpProbe(
        "Seerr status",
        "/api/v1/status",
        required_json_key="version",
    ),
    "pgadmin": HttpProbe("pgAdmin ping", "/misc/ping"),
    "traefik_proxy_admin": HttpProbe(
        "Traefik Proxy Admin health",
        "/api/health",
    ),
    "authelia": HttpProbe("Authelia health", "/api/health"),
    "aiostreams": HttpProbe(
        "AIOStreams health", "/api/v1/health", required_json_key="success"
    ),
    "mediastorm": HttpProbe("mediastorm health", "/health"),
}


def _safe_string(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return f"{text[: limit - 1]}…"
    return text


def _normalize_reported_state(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, bool):
        return ("healthy", "true") if value else ("unhealthy", "false")
    if value is None or isinstance(value, (dict, list)):
        return None, None

    reported = _safe_string(value, 80)
    normalized = (
        reported.strip().lower().replace("-", "_").replace(" ", "_").rstrip(".")
    )
    if normalized in HEALTHY_STATES:
        return "healthy", reported
    if normalized in STARTING_STATES:
        return "starting", reported
    if normalized in DEGRADED_STATES:
        return "degraded", reported
    if normalized in UNHEALTHY_STATES:
        return "unhealthy", reported
    return None, reported


def _status_from_payload(payload: Any) -> tuple[str | None, str | None]:
    if isinstance(payload, str):
        return _normalize_reported_state(payload)
    if not isinstance(payload, dict):
        return None, None

    for key in (
        "status",
        "state",
        "health",
        "overallStatus",
        "overall_status",
    ):
        if key in payload:
            status, reported = _normalize_reported_state(payload.get(key))
            if status or reported:
                return status, reported
    return None, None


def _component_entries(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        return []

    container = None
    for key in ("checks", "entries", "components", "dependencies", "results"):
        candidate = payload.get(key)
        if isinstance(candidate, (dict, list)):
            container = candidate
            break
    if container is None:
        return []

    raw_entries: list[tuple[Any, Any]] = []
    if isinstance(container, dict):
        raw_entries.extend(container.items())
    else:
        for index, item in enumerate(container):
            name = item.get("name") if isinstance(item, dict) else index
            raw_entries.append((name, item))

    entries = []
    for raw_name, raw_value in raw_entries[:MAX_COMPONENTS]:
        if isinstance(raw_value, dict):
            name = raw_value.get("name") or raw_value.get("key") or raw_name
            raw_status = (
                raw_value.get("status")
                if "status" in raw_value
                else raw_value.get("state")
            )
        else:
            name = raw_name
            raw_status = raw_value
        status, reported = _normalize_reported_state(raw_status)
        if not reported:
            continue
        entries.append(
            {
                "name": _safe_string(name, 80),
                "status": status or _safe_string(reported, 40).lower(),
            }
        )
    return entries


def _combine_payload_status(
    overall_status: str | None,
    components: list[dict[str, str]],
) -> str | None:
    if overall_status:
        return overall_status
    component_states = {entry["status"] for entry in components}
    if "unhealthy" in component_states:
        return "unhealthy"
    if "starting" in component_states:
        return "starting"
    if "degraded" in component_states:
        return "degraded"
    if component_states and component_states <= {"healthy"}:
        return "healthy"
    if component_states:
        return "degraded"
    return None


def _result(
    status: str,
    reason: str | None,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": status,
        "healthy": status in {"healthy", "degraded", "starting"},
        "reason": reason,
        "details": details,
    }


class ServiceHealthMonitor:
    """Runs and briefly caches safe application-level service probes."""

    def __init__(self, logger=None, cache_ttl_seconds=10, timeout_seconds=2.5):
        self.logger = logger
        self.cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self.timeout_seconds = max(0.25, float(timeout_seconds))
        self._cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()

    def check(
        self,
        config_key: str | None,
        process_name: str,
        config: dict[str, Any] | None,
        process_identity: Any = None,
    ) -> dict[str, Any] | None:
        key = str(config_key or "").strip().lower()
        config = config if isinstance(config, dict) else {}
        probe = self._resolve_probe(key, config)
        if probe is None:
            return None

        cache_key = (
            key,
            process_name,
            process_identity,
            probe.get("cache_identity"),
        )
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and now - cached[0] < self.cache_ttl_seconds:
                return cached[1]

        if probe["kind"] == "postgres":
            result = self._probe_postgres(process_name, config)
        else:
            result = self._probe_http(process_name, probe)

        with self._cache_lock:
            self._cache[cache_key] = (now, result)
            if len(self._cache) > 256:
                cutoff = now - max(self.cache_ttl_seconds, 1.0)
                self._cache = {
                    item_key: item
                    for item_key, item in self._cache.items()
                    if item[0] >= cutoff
                }
        return result

    def _resolve_probe(
        self,
        config_key: str,
        config: dict[str, Any],
    ) -> dict[str, Any] | None:
        if config_key == "postgres":
            port = self._config_port(config, "port")
            if port is None:
                return None
            return {
                "kind": "postgres",
                "cache_identity": ("postgres", port),
            }

        if config_key == "rclone":
            return self._resolve_rclone_probe(config)

        spec = HTTP_PROBES.get(config_key)
        if config_key in SERVARR_KEYS:
            spec = HttpProbe("Servarr application ping", "/ping")
        if spec is None:
            return None

        port = self._config_port(config, spec.port_field)
        if port is None:
            return None
        return {
            "kind": "http",
            "name": spec.name,
            "path": spec.path,
            "port": port,
            "method": spec.method,
            "required_json_key": spec.required_json_key,
            "required_text": spec.required_text,
            "auth": None,
            "cache_identity": (
                spec.name,
                spec.path,
                port,
                spec.method,
            ),
        }

    @staticmethod
    def _config_port(config: dict[str, Any], field: str) -> int | None:
        value = config.get(field)
        if isinstance(value, int) and 0 < value <= 65535:
            return value
        if isinstance(value, str) and value.isdigit():
            port = int(value)
            return port if 0 < port <= 65535 else None
        return None

    def _resolve_rclone_probe(
        self,
        config: dict[str, Any],
    ) -> dict[str, Any] | None:
        command = config.get("command")
        if not isinstance(command, list) or not self._command_flag_enabled(
            command,
            "--rc",
        ):
            return None

        address = self._command_option(command, "--rc-addr") or "127.0.0.1:5572"
        address = address.rsplit("://", 1)[-1]
        if address.startswith("[") and "]:" in address:
            host, port_text = address[1:].split("]:", 1)
        elif ":" in address:
            host, port_text = address.rsplit(":", 1)
        else:
            host, port_text = "127.0.0.1", address
        if host.strip().lower() not in {"", "0.0.0.0", "127.0.0.1", "::", "localhost"}:
            return None
        if not port_text.isdigit() or not (0 < int(port_text) <= 65535):
            return None

        username = self._command_option(command, "--rc-user")
        password = self._command_option(command, "--rc-pass")
        auth = (username, password or "") if username else None
        port = int(port_text)
        return {
            "kind": "http",
            "name": "rclone RC version",
            "path": "/core/version",
            "port": port,
            "method": "POST",
            "required_json_key": "version",
            "required_text": None,
            "auth": auth,
            "cache_identity": ("rclone RC version", port, bool(auth)),
        }

    @staticmethod
    def _command_option(command: list[Any], option: str) -> str | None:
        for index, raw_value in enumerate(command):
            value = str(raw_value)
            if value == option and index + 1 < len(command):
                return str(command[index + 1])
            if value.startswith(f"{option}="):
                return value.split("=", 1)[1]
        return None

    @staticmethod
    def _command_flag_enabled(command: list[Any], option: str) -> bool:
        truthy = {"1", "on", "true", "yes"}
        falsey = {"0", "false", "no", "off"}
        for index, raw_value in enumerate(command):
            value = str(raw_value).strip()
            if value == option:
                if index + 1 < len(command):
                    next_value = str(command[index + 1]).strip().lower()
                    if next_value in truthy | falsey:
                        return next_value in truthy
                return True
            if value.startswith(f"{option}="):
                flag_value = value.split("=", 1)[1].strip().lower()
                return flag_value in truthy
        return False

    def _probe_http(
        self,
        process_name: str,
        probe: dict[str, Any],
    ) -> dict[str, Any]:
        started = time.monotonic()
        details = {
            "probe": probe["name"],
            "endpoint": probe["path"],
            "supported": True,
        }
        url = f"http://127.0.0.1:{probe['port']}{probe['path']}"
        try:
            with requests.request(
                probe["method"],
                url,
                auth=probe.get("auth"),
                allow_redirects=False,
                stream=True,
                timeout=self.timeout_seconds,
            ) as response:
                body = response.raw.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
                details["http_status"] = response.status_code
                details["latency_ms"] = round((time.monotonic() - started) * 1000)
                if len(body) > MAX_RESPONSE_BYTES:
                    details["response_truncated"] = True
                    return _result(
                        "degraded",
                        f"{process_name} health response exceeded the safe size limit",
                        details,
                    )
                content_type = response.headers.get("content-type", "")
        except requests.RequestException as error:
            details["latency_ms"] = round((time.monotonic() - started) * 1000)
            details["error_type"] = type(error).__name__
            return _result(
                "unhealthy",
                f"{process_name} application health endpoint did not respond",
                details,
            )

        text = body.decode("utf-8", errors="replace").strip()
        payload: Any = text
        if text and ("json" in content_type.lower() or text[:1] in "[{"):
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                payload = text

        reported_status, reported = _status_from_payload(payload)
        components = _component_entries(payload)
        status = _combine_payload_status(reported_status, components)
        if reported:
            details["reported_status"] = reported
        if components:
            details["components"] = components

        response_status = int(details["http_status"])
        if 200 <= response_status < 300:
            validation_failure = self._validate_success_payload(probe, payload, text)
            if validation_failure:
                return _result(
                    "degraded",
                    f"{process_name} health endpoint returned an unexpected response",
                    {**details, "validation": validation_failure},
                )
            if probe.get("required_text") and status is None:
                reported = None
                details.pop("reported_status", None)
                final_status = "healthy"
            else:
                final_status = status or ("degraded" if reported else "healthy")
        elif response_status in {401, 403, 404, 405, 429}:
            details["supported"] = response_status != 404
            final_status = status or "degraded"
        elif 300 <= response_status < 500:
            final_status = status or "degraded"
        else:
            final_status = status or "unhealthy"

        reason = self._health_reason(process_name, final_status, reported, components)
        return _result(final_status, reason, details)

    @staticmethod
    def _validate_success_payload(
        probe: dict[str, Any],
        payload: Any,
        text: str,
    ) -> str | None:
        required_key = probe.get("required_json_key")
        if required_key:
            value = payload.get(required_key) if isinstance(payload, dict) else None
            if value is None or value == "":
                return f"missing {required_key}"
        required_text = probe.get("required_text")
        if required_text and required_text.lower() not in text.lower():
            return "identity marker missing"
        return None

    @staticmethod
    def _health_reason(
        process_name: str,
        status: str,
        reported: str | None,
        components: list[dict[str, str]],
    ) -> str | None:
        if status == "healthy":
            return None

        affected = [
            entry["name"]
            for entry in components
            if entry["status"] in {"degraded", "starting", "unhealthy"}
        ]
        suffix = f": {', '.join(affected[:4])}" if affected else ""
        if status == "starting":
            state = reported or "starting"
            return f"{process_name} reports {state}{suffix}"
        if status == "degraded":
            state = reported or "a degraded application health state"
            return f"{process_name} reports {state}{suffix}"
        state = reported or "an unhealthy application state"
        return f"{process_name} reports {state}{suffix}"

    def _probe_postgres(
        self,
        process_name: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        port = self._config_port(config, "port")
        postgres_user = str(config.get("user") or "DUMB")
        executable = shutil.which("pg_isready")
        if executable is None:
            candidates = sorted(
                path
                for path in (
                    "/usr/lib/postgresql/16/bin/pg_isready",
                    "/usr/lib/postgresql/17/bin/pg_isready",
                    "/usr/lib/postgresql/18/bin/pg_isready",
                )
                if os.path.isfile(path)
            )
            executable = candidates[-1] if candidates else None
        details = {
            "probe": "PostgreSQL readiness",
            "endpoint": "local pg_isready",
            "supported": executable is not None,
        }
        if executable is None or port is None:
            return _result(
                "degraded",
                "PostgreSQL readiness probe is unavailable; using process and port checks",
                details,
            )

        started = time.monotonic()
        try:
            completed = subprocess.run(
                [
                    executable,
                    "-U",
                    postgres_user,
                    "-d",
                    "postgres",
                    "-h",
                    "127.0.0.1",
                    "-p",
                    str(port),
                    "-t",
                    str(max(1, round(self.timeout_seconds))),
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds + 1,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            details["latency_ms"] = round((time.monotonic() - started) * 1000)
            details["error_type"] = type(error).__name__
            return _result(
                "unhealthy",
                f"{process_name} readiness probe did not complete",
                details,
            )

        details["latency_ms"] = round((time.monotonic() - started) * 1000)
        details["exit_code"] = completed.returncode
        if completed.returncode == 0:
            return _result("healthy", None, details)
        if completed.returncode == 1:
            return _result(
                "starting",
                f"{process_name} is running but rejecting connections",
                details,
            )
        if completed.returncode == 2:
            return _result(
                "unhealthy",
                f"{process_name} is not accepting database connections",
                details,
            )
        return _result(
            "degraded",
            f"{process_name} readiness probe configuration is invalid",
            details,
        )
