import copy
import json
import threading
import time
from urllib.parse import urlparse

from utils.url_security import safe_request, safe_urlopen

PLEX_STATUS_PAGE_URL = "https://status.plex.tv/"
PLEX_STATUS_SUMMARY_URL = "https://status.plex.tv/api/v2/summary.json"
DEFAULT_INTERVAL_SEC = 300
MIN_INTERVAL_SEC = 60
MAX_INTERVAL_SEC = 3600
REQUEST_TIMEOUT_SEC = 10
MAX_RESPONSE_BYTES = 1024 * 1024


def _text(value, limit=500):
    text = str(value or "").strip()
    return text[:limit]


def _https_url(value):
    url = _text(value, 500)
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return url


class PlexStatusCollector:
    def __init__(self, logger=None):
        self.logger = logger
        self._lock = threading.Lock()
        self._cached = None
        self._cached_monotonic = 0.0
        self._last_attempt_monotonic = 0.0
        self._refreshing = False
        self._last_error = None
        self._refresh_done = threading.Event()
        self._refresh_done.set()

    def invalidate(self):
        with self._lock:
            self._last_attempt_monotonic = 0.0

    def snapshot(self, config, refresh_if_stale=False, wait_for_refresh=False):
        status_config = ((config or {}).get("dumb", {}) or {}).get("metrics", {}).get(
            "plex_status", {}
        ) or {}
        enabled = status_config.get("enabled") is True
        interval_sec = self._interval(status_config.get("interval_sec"))
        if not enabled:
            return {
                "enabled": False,
                "available": False,
                "stale": False,
                "indicator": "disabled",
                "description": "Plex cloud status monitoring is disabled.",
                "source_url": PLEX_STATUS_PAGE_URL,
                "interval_sec": interval_sec,
            }

        with self._lock:
            now_monotonic = time.monotonic()
            cache_age = now_monotonic - self._cached_monotonic
            attempt_age = now_monotonic - self._last_attempt_monotonic
            should_refresh = self._last_attempt_monotonic <= 0 or (
                refresh_if_stale and attempt_age >= interval_sec
            )
            if not should_refresh:
                if self._cached is None:
                    return self._unavailable_result(
                        interval_sec,
                        refreshing=self._refreshing,
                        error=self._last_error,
                    )
                result = self._with_cache_metadata(
                    self._cached,
                    cache_age,
                    interval_sec,
                    stale=self._last_error is not None,
                    refreshing=self._refreshing,
                )
                if self._last_error:
                    result["error"] = self._last_error
                return result
            refresh_started = not self._refreshing
            if refresh_started:
                self._refreshing = True
                self._last_attempt_monotonic = now_monotonic
                self._refresh_done.clear()

        refresh_succeeded = None
        if refresh_started and wait_for_refresh:
            refresh_succeeded = self._run_refresh()
        elif refresh_started:
            threading.Thread(
                target=self._run_refresh,
                name="plex-status-refresh",
                daemon=True,
            ).start()
        elif wait_for_refresh:
            self._refresh_done.wait(timeout=REQUEST_TIMEOUT_SEC + 1)
            with self._lock:
                refresh_succeeded = (
                    self._cached is not None and self._last_error is None
                )

        with self._lock:
            if self._cached is not None:
                cache_age = max(0.0, time.monotonic() - self._cached_monotonic)
                result = self._with_cache_metadata(
                    self._cached,
                    cache_age,
                    interval_sec,
                    stale=refresh_succeeded is not True,
                    refreshing=self._refreshing,
                )
                if self._last_error:
                    result["error"] = self._last_error
                return result
            return self._unavailable_result(
                interval_sec,
                refreshing=self._refreshing,
                error=self._last_error,
            )

    def _run_refresh(self):
        started = time.monotonic()
        succeeded = False
        try:
            payload = self._fetch()
            result = self._normalize(payload)
            result["response_ms"] = round((time.monotonic() - started) * 1000, 1)
            with self._lock:
                self._cached = result
                self._cached_monotonic = time.monotonic()
                self._last_error = None
                succeeded = True
        except Exception as exc:
            self._log_error(exc)
            with self._lock:
                self._last_error = (
                    "The Plex status feed could not be refreshed; showing the "
                    "last successful result."
                    if self._cached is not None
                    else "The Plex status feed could not be loaded."
                )
        finally:
            with self._lock:
                self._refreshing = False
            self._refresh_done.set()
        return succeeded

    def _fetch(self):
        request = safe_request(
            PLEX_STATUS_SUMMARY_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "DUMB Plex Status Metric",
            },
        )
        with safe_urlopen(request, timeout=REQUEST_TIMEOUT_SEC) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise RuntimeError("Unexpected Plex status response")
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("Plex status response exceeded the size limit")
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Plex status response was not an object")
        return payload

    def _normalize(self, payload):
        status = (
            payload.get("status") if isinstance(payload.get("status"), dict) else {}
        )
        page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
        components = (
            payload.get("components")
            if isinstance(payload.get("components"), list)
            else []
        )
        incidents = (
            payload.get("incidents")
            if isinstance(payload.get("incidents"), list)
            else []
        )
        maintenances = (
            payload.get("scheduled_maintenances")
            if isinstance(payload.get("scheduled_maintenances"), list)
            else []
        )

        component_status_counts = {}
        affected_components = []
        for component in components:
            if not isinstance(component, dict):
                continue
            component_status = _text(component.get("status"), 80) or "unknown"
            component_status_counts[component_status] = (
                component_status_counts.get(component_status, 0) + 1
            )
            if component_status == "operational":
                continue
            affected_components.append(
                {
                    "id": _text(component.get("id"), 100),
                    "name": _text(component.get("name"), 200),
                    "status": component_status,
                    "group_id": _text(component.get("group_id"), 100) or None,
                }
            )

        active_incidents = [
            self._normalize_event(item) for item in incidents if isinstance(item, dict)
        ]
        scheduled_maintenances = [
            self._normalize_event(item)
            for item in maintenances
            if isinstance(item, dict)
        ]
        indicator = _text(status.get("indicator"), 80) or "unknown"
        fetched_at = time.time()
        return {
            "enabled": True,
            "available": True,
            "stale": False,
            "operational": (
                indicator == "none" and not affected_components and not active_incidents
            ),
            "indicator": indicator,
            "description": _text(status.get("description"), 300)
            or "Plex status is available.",
            "source_url": PLEX_STATUS_PAGE_URL,
            "source_updated_at": _text(page.get("updated_at"), 100) or None,
            "fetched_at": fetched_at,
            "affected_components": affected_components,
            "active_incidents": active_incidents,
            "scheduled_maintenances": scheduled_maintenances,
            "component_status_counts": dict(sorted(component_status_counts.items())),
        }

    @staticmethod
    def _normalize_event(event):
        event_components = (
            event.get("components") if isinstance(event.get("components"), list) else []
        )
        return {
            "id": _text(event.get("id"), 100),
            "name": _text(event.get("name"), 300),
            "status": _text(event.get("status"), 80) or "unknown",
            "impact": _text(event.get("impact"), 80) or "unknown",
            "updated_at": _text(event.get("updated_at"), 100) or None,
            "scheduled_for": _text(event.get("scheduled_for"), 100) or None,
            "scheduled_until": _text(event.get("scheduled_until"), 100) or None,
            "shortlink": _https_url(event.get("shortlink")),
            "components": [
                {
                    "name": _text(component.get("name"), 200),
                    "status": _text(component.get("status"), 80) or "unknown",
                }
                for component in event_components
                if isinstance(component, dict)
            ],
        }

    @staticmethod
    def _interval(value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = DEFAULT_INTERVAL_SEC
        return max(MIN_INTERVAL_SEC, min(MAX_INTERVAL_SEC, parsed))

    @staticmethod
    def _with_cache_metadata(result, cache_age, interval_sec, stale, refreshing=False):
        output = copy.deepcopy(result)
        output["enabled"] = True
        output["stale"] = stale
        output["refreshing"] = refreshing
        output["interval_sec"] = interval_sec
        output["cache_age_sec"] = round(max(0.0, cache_age), 1)
        return output

    @staticmethod
    def _unavailable_result(interval_sec, refreshing=False, error=None):
        return {
            "enabled": True,
            "available": False,
            "stale": False,
            "refreshing": refreshing,
            "indicator": "collecting" if refreshing else "unavailable",
            "description": (
                "Collecting Plex cloud status."
                if refreshing
                else "The Plex status feed is currently unavailable."
            ),
            "source_url": PLEX_STATUS_PAGE_URL,
            "interval_sec": interval_sec,
            "fetched_at": None,
            "cache_age_sec": None,
            "affected_components": [],
            "active_incidents": [],
            "scheduled_maintenances": [],
            "component_status_counts": {},
            **({"error": error} if error else {}),
        }

    def _log_error(self, exc):
        if self.logger is None:
            return
        self.logger.warning(
            "Unable to refresh the Plex cloud status metric: %s",
            type(exc).__name__,
        )
