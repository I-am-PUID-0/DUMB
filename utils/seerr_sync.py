"""
Seerr Sync - One-way request replication from primary to subordinate Seerr instances.

This module polls a primary Seerr instance for media requests and replicates them
to any number of subordinate instances. Each subordinate operates independently
with its own Arr stack configuration.
"""

from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
from typing import Optional
import json, os, time, threading, urllib.request, urllib.error


_SYNC_STATE_FILE = "/config/seerr_sync_state.json"

# Request status constants from Seerr API
REQUEST_STATUS_PENDING = 1
REQUEST_STATUS_APPROVED = 2
REQUEST_STATUS_DECLINED = 3


def _load_sync_state() -> dict:
    """Load persistent sync state from file."""
    if os.path.exists(_SYNC_STATE_FILE):
        try:
            with open(_SYNC_STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Failed to load seerr sync state: %s", e)
    return {"last_poll_ts": None, "requests": {}, "failed": {}}


def _save_sync_state(state: dict) -> None:
    """Save sync state to file."""
    try:
        os.makedirs(os.path.dirname(_SYNC_STATE_FILE), exist_ok=True)
        with open(_SYNC_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save seerr sync state: %s", e)


def _seerr_req(
    url: str,
    api_key: str,
    method: str = "GET",
    data: Optional[dict] = None,
    timeout: int = 30,
) -> Optional[dict]:
    """Make a request to a Seerr instance."""
    headers = {
        "X-Api-Key": api_key,
        "Accept": "application/json",
    }
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8")
        except Exception:
            error_body = ""
        logger.debug("Seerr API error %s from %s: %s", e.code, url, error_body)
        raise
    except Exception as e:
        logger.debug("Seerr request failed to %s: %s", url, e)
        raise


def _join_url(base: str, path: str) -> str:
    """Join base URL with path."""
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _request_fingerprint(request: dict) -> str:
    """Generate a unique fingerprint for a request based on media info."""
    media = request.get("media", {})
    media_type = media.get("mediaType", "unknown")
    tmdb_id = media.get("tmdbId", 0)
    is_4k = request.get("is4k", False)
    return f"{media_type}:{tmdb_id}:{is_4k}"


def _get_seerr_connection(instance_name: str) -> Optional[tuple[str, str, int]]:
    """Get connection info for an internal Seerr instance.

    Returns (url, api_key, port) or None if not available.
    """
    seerr_cfg = CONFIG_MANAGER.get("seerr", {})
    instances = seerr_cfg.get("instances", {})
    inst = instances.get(instance_name, {})

    if not inst.get("enabled"):
        return None

    port = inst.get("port")
    if not port:
        return None

    # Read API key from Seerr settings file
    config_file = inst.get("config_file", "")
    api_key = _read_seerr_api_key(config_file)
    if not api_key:
        return None

    url = f"http://127.0.0.1:{port}"
    return url, api_key, port


def _read_seerr_api_key(config_file: str) -> str:
    """Read API key from Seerr settings.json."""
    if not config_file or not os.path.exists(config_file):
        return ""
    try:
        with open(config_file, "r") as f:
            settings = json.load(f)
        # Seerr stores API key in main.apiKey
        main = settings.get("main", {})
        return main.get("apiKey", "")
    except Exception as e:
        logger.debug("Failed to read Seerr API key from %s: %s", config_file, e)
        return ""


def _wait_for_seerr(url: str, api_key: str, timeout_s: int = 30) -> bool:
    """Wait for a Seerr instance to be ready."""
    deadline = time.time() + max(1, timeout_s)
    status_url = _join_url(url, "/api/v1/status")
    while time.time() < deadline:
        try:
            _seerr_req(status_url, api_key, timeout=5)
            return True
        except Exception:
            time.sleep(2)
    return False


def _poll_requests(url: str, api_key: str, options: dict) -> list[dict]:
    """Poll all requests from a Seerr instance."""
    requests = []
    take = 100
    skip = 0

    while True:
        try:
            endpoint = _join_url(
                url, f"/api/v1/request?take={take}&skip={skip}&sort=modified"
            )
            response = _seerr_req(endpoint, api_key)
            if not response:
                break

            results = response.get("results", [])
            if not results:
                break

            for req in results:
                # Filter by status based on options
                status = req.get("status")
                if status == REQUEST_STATUS_PENDING and not options.get(
                    "sync_pending", True
                ):
                    continue
                if status == REQUEST_STATUS_APPROVED and not options.get(
                    "sync_approved", True
                ):
                    continue
                if status == REQUEST_STATUS_DECLINED and not options.get(
                    "sync_declined", False
                ):
                    continue

                requests.append(req)

            # Check if there are more pages
            page_info = response.get("pageInfo", {})
            total_results = page_info.get("results", 0)
            if skip + len(results) >= total_results:
                break
            skip += take

        except Exception as e:
            logger.warning("Failed to poll requests from %s: %s", url, e)
            break

    return requests


def _create_request(
    url: str, api_key: str, primary_request: dict
) -> tuple[Optional[int], Optional[str]]:
    """Create a new request on a subordinate instance.

    Returns (request_id, error_message). If successful, error_message is None.
    If failed, request_id is None and error_message contains the reason.
    """
    media = primary_request.get("media", {})
    media_type = media.get("mediaType")
    tmdb_id = media.get("tmdbId")

    if not media_type or not tmdb_id:
        return None, "missing media type or TMDB ID"

    payload = {
        "mediaType": media_type,
        "mediaId": tmdb_id,
        "is4k": primary_request.get("is4k", False),
    }

    # For TV shows, include seasons if specified
    if media_type == "tv":
        seasons = primary_request.get("seasons", [])
        if seasons:
            # Extract season numbers from season objects, filtering out None
            season_numbers = []
            for s in seasons:
                if isinstance(s, dict):
                    sn = s.get("seasonNumber")
                    if sn is not None:
                        season_numbers.append(sn)
                elif isinstance(s, int):
                    season_numbers.append(s)
            if season_numbers:
                payload["seasons"] = season_numbers
            # If we have seasons but all were None, don't include empty array
            # Seerr may fail with empty seasons array

    try:
        endpoint = _join_url(url, "/api/v1/request")
        response = _seerr_req(endpoint, api_key, method="POST", data=payload)
        if response:
            req_id = response.get("id")
            if req_id:
                return req_id, None
            # Response exists but no id - might indicate partial success or unexpected response
            return None, f"response missing id: {json.dumps(response)[:200]}"
        return None, "empty response"
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8")
            error_data = json.loads(error_body)
            error_msg = error_data.get("message", f"HTTP {e.code}")
        except Exception:
            error_msg = f"HTTP {e.code}"

        if e.code == 409:
            # Conflict - request already exists, not really an error
            return None, "already_exists"
        return None, error_msg
    except Exception as e:
        return None, str(e)


def _update_request_status(
    url: str, api_key: str, request_id: int, status: int
) -> bool:
    """Update a request's status (approve/decline)."""
    try:
        if status == REQUEST_STATUS_APPROVED:
            endpoint = _join_url(url, f"/api/v1/request/{request_id}/approve")
        elif status == REQUEST_STATUS_DECLINED:
            endpoint = _join_url(url, f"/api/v1/request/{request_id}/decline")
        else:
            return False

        _seerr_req(endpoint, api_key, method="POST")
        return True
    except Exception as e:
        logger.warning("Failed to update request status: %s", e)
        return False


def _delete_request(url: str, api_key: str, request_id: int) -> bool:
    """Delete a request from a subordinate instance."""
    try:
        endpoint = _join_url(url, f"/api/v1/request/{request_id}")
        _seerr_req(endpoint, api_key, method="DELETE")
        return True
    except Exception as e:
        logger.warning("Failed to delete request: %s", e)
        return False


def _get_subordinate_label(sub: dict) -> str:
    """Get a human-readable label for a subordinate."""
    if sub.get("type") == "internal":
        return f"internal:{sub.get('instance', 'unknown')}"
    return sub.get("url", "unknown")


def validate_seerr_sync_config() -> list[str]:
    """Validate the seerr_sync configuration. Returns list of errors."""
    errors = []

    sync_cfg = CONFIG_MANAGER.get("seerr_sync", {})
    if not sync_cfg.get("enabled"):
        return []  # Sync disabled, no validation needed

    seerr_cfg = CONFIG_MANAGER.get("seerr", {})
    instances = seerr_cfg.get("instances", {})
    external_primary = sync_cfg.get("external_primary", {})
    external_primary_enabled = external_primary.get("enabled", False)

    # Collect roles from internal instances
    primaries = []
    subordinates = []

    for name, inst in instances.items():
        if not inst.get("enabled"):
            continue
        role = inst.get("sync_role", "disabled")
        if role == "primary":
            primaries.append(name)
        elif role == "subordinate":
            subordinates.append(name)

    # Rule 1: Only one primary allowed
    if len(primaries) > 1:
        errors.append(f"Multiple primaries defined: {primaries}. Only one allowed.")

    # Rule 2: If external primary, no internal primary allowed
    if external_primary_enabled and primaries:
        errors.append(
            f"External primary enabled but internal primary also defined: {primaries}"
        )

    # Rule 3: Must have exactly one primary (internal or external)
    if not external_primary_enabled and len(primaries) == 0:
        errors.append(
            "No primary defined. Set one instance to sync_role='primary' or enable external_primary."
        )

    # Rule 4: Validate external primary has required fields
    if external_primary_enabled:
        if not external_primary.get("url"):
            errors.append("External primary enabled but URL is not set.")
        if not external_primary.get("api_key"):
            errors.append("External primary enabled but API key is not set.")

    # Rule 5: Must have at least one subordinate
    external_subs = sync_cfg.get("external_subordinates", [])
    if not subordinates and not external_subs:
        errors.append(
            "No subordinates defined. Need at least one internal or external subordinate."
        )

    # Rule 6: Validate external subordinates have required fields
    for i, sub in enumerate(external_subs):
        if not sub.get("url"):
            errors.append(f"External subordinate {i+1} is missing URL.")
        if not sub.get("api_key"):
            errors.append(f"External subordinate {i+1} is missing API key.")

    return errors


def _get_primary_connection(sync_cfg: dict) -> Optional[tuple[str, str]]:
    """Get the primary Seerr connection (url, api_key)."""
    external_primary = sync_cfg.get("external_primary", {})

    if external_primary.get("enabled"):
        url = external_primary.get("url", "").rstrip("/")
        api_key = external_primary.get("api_key", "")
        if url and api_key:
            return url, api_key
        return None

    # Find internal primary
    seerr_cfg = CONFIG_MANAGER.get("seerr", {})
    instances = seerr_cfg.get("instances", {})

    for name, inst in instances.items():
        if not inst.get("enabled"):
            continue
        if inst.get("sync_role") == "primary":
            conn = _get_seerr_connection(name)
            if conn:
                return conn[0], conn[1]

    return None


def _get_subordinate_connections(sync_cfg: dict) -> list[dict]:
    """Get all subordinate connections with metadata."""
    subordinates = []

    # Internal subordinates
    seerr_cfg = CONFIG_MANAGER.get("seerr", {})
    instances = seerr_cfg.get("instances", {})

    for name, inst in instances.items():
        if not inst.get("enabled"):
            continue
        if inst.get("sync_role") == "subordinate":
            conn = _get_seerr_connection(name)
            if conn:
                subordinates.append(
                    {
                        "type": "internal",
                        "instance": name,
                        "url": conn[0],
                        "api_key": conn[1],
                    }
                )

    # External subordinates
    external_subs = sync_cfg.get("external_subordinates", [])
    for sub in external_subs:
        url = sub.get("url", "").rstrip("/")
        api_key = sub.get("api_key", "")
        if url and api_key:
            subordinates.append(
                {
                    "type": "external",
                    "url": url,
                    "api_key": api_key,
                }
            )

    return subordinates


def run_sync_cycle() -> None:
    """Run a single sync cycle."""
    sync_cfg = CONFIG_MANAGER.get("seerr_sync", {})

    if not sync_cfg.get("enabled"):
        return

    # Validate config
    errors = validate_seerr_sync_config()
    if errors:
        for error in errors:
            logger.error("Seerr sync config error: %s", error)
        return

    options = sync_cfg.get("options", {})
    state = _load_sync_state()

    # Get primary connection
    primary_conn = _get_primary_connection(sync_cfg)
    if not primary_conn:
        logger.warning("Seerr sync: Could not connect to primary instance")
        return

    primary_url, primary_api_key = primary_conn

    # Wait for primary to be ready
    if not _wait_for_seerr(primary_url, primary_api_key, timeout_s=10):
        logger.debug("Seerr sync: Primary instance not ready, skipping cycle")
        return

    # Get subordinate connections
    subordinates = _get_subordinate_connections(sync_cfg)
    if not subordinates:
        logger.warning("Seerr sync: No subordinates available")
        return

    # Poll requests from primary
    primary_requests = _poll_requests(primary_url, primary_api_key, options)

    # Build fingerprint map of current primary requests
    primary_fingerprints = {}
    for req in primary_requests:
        fp = _request_fingerprint(req)
        primary_fingerprints[fp] = req

    # Track sync statistics per subordinate
    total_requests = len(primary_requests)

    # Get retry settings
    retry_failed_after_hours = options.get("retry_failed_after_hours", 24)

    # Sync to each subordinate
    for sub in subordinates:
        sub_label = _get_subordinate_label(sub)
        sub_url = sub["url"]
        sub_api_key = sub["api_key"]

        # Stats for this subordinate
        stats = {
            "new": 0,
            "already_synced": 0,
            "status_updated": 0,
            "deleted": 0,
            "failed": 0,
            "skipped_failed": 0,
        }

        # Wait for subordinate to be ready
        if not _wait_for_seerr(sub_url, sub_api_key, timeout_s=10):
            logger.warning("Seerr sync: Subordinate %s not ready, skipping", sub_label)
            continue

        # Process each primary request
        for fp, req in primary_fingerprints.items():
            req_state = state.get("requests", {}).get(fp, {})
            sub_state = req_state.get("subordinates", {}).get(sub_label, {})

            # Check if already synced
            existing_id = sub_state.get("id")
            existing_status = sub_state.get("status")
            current_status = req.get("status")

            if existing_id:
                # Request was already synced, check for status changes
                if existing_status != current_status:
                    if current_status in (
                        REQUEST_STATUS_APPROVED,
                        REQUEST_STATUS_DECLINED,
                    ):
                        if _update_request_status(
                            sub_url, sub_api_key, existing_id, current_status
                        ):
                            status_name = (
                                "approved"
                                if current_status == REQUEST_STATUS_APPROVED
                                else "declined"
                            )
                            media = req.get("media", {})
                            logger.info(
                                "Seerr sync: Status changed to %s on %s for %s (tmdb=%s)",
                                status_name,
                                sub_label,
                                media.get("mediaType", "unknown"),
                                media.get("tmdbId", "?"),
                            )
                            stats["status_updated"] += 1
                            # Update state
                            if fp not in state.get("requests", {}):
                                state.setdefault("requests", {})[fp] = {
                                    "subordinates": {}
                                }
                            state["requests"][fp].setdefault("subordinates", {})[
                                sub_label
                            ] = {
                                "id": existing_id,
                                "status": current_status,
                                "synced_at": time.strftime(
                                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                ),
                            }
                    else:
                        stats["already_synced"] += 1
                else:
                    stats["already_synced"] += 1
            else:
                # Check if this request previously failed for this subordinate
                # Use || as separator since fingerprints contain colons
                failed_key = f"{fp}||{sub_label}"
                failed_info = state.get("failed", {}).get(failed_key, {})
                if failed_info:
                    failed_at = failed_info.get("failed_at", "")
                    # Check if we should retry
                    if failed_at and retry_failed_after_hours > 0:
                        try:
                            failed_time = time.mktime(
                                time.strptime(failed_at, "%Y-%m-%dT%H:%M:%SZ")
                            )
                            hours_since_failure = (time.time() - failed_time) / 3600
                            if hours_since_failure < retry_failed_after_hours:
                                stats["skipped_failed"] += 1
                                continue
                        except Exception:
                            pass  # If we can't parse time, try again

                # New request, create on subordinate
                media = req.get("media", {})
                new_id, error_msg = _create_request(sub_url, sub_api_key, req)

                if new_id:
                    logger.info(
                        "Seerr sync: New request synced to %s for %s (tmdb=%s)",
                        sub_label,
                        media.get("mediaType", "unknown"),
                        media.get("tmdbId", "?"),
                    )
                    stats["new"] += 1

                    # Clear any previous failure
                    state.get("failed", {}).pop(failed_key, None)

                    # Update state
                    if fp not in state.get("requests", {}):
                        state.setdefault("requests", {})[fp] = {
                            "primary_id": req.get("id"),
                            "subordinates": {},
                        }
                    state["requests"][fp]["primary_id"] = req.get("id")
                    state["requests"][fp].setdefault("subordinates", {})[sub_label] = {
                        "id": new_id,
                        "status": current_status,
                        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }

                    # If the request is already approved on primary, approve on subordinate too
                    if current_status == REQUEST_STATUS_APPROVED:
                        _update_request_status(
                            sub_url, sub_api_key, new_id, REQUEST_STATUS_APPROVED
                        )

                elif error_msg == "already_exists":
                    # Not really a failure, just already there
                    stats["already_synced"] += 1
                else:
                    # Track failure
                    stats["failed"] += 1
                    state.setdefault("failed", {})[failed_key] = {
                        "failed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "error": error_msg,
                        "media_type": media.get("mediaType", "unknown"),
                        "tmdb_id": media.get("tmdbId"),
                    }
                    logger.warning(
                        "Seerr sync: Failed to sync %s (tmdb=%s) to %s: %s",
                        media.get("mediaType", "unknown"),
                        media.get("tmdbId", "?"),
                        sub_label,
                        error_msg,
                    )

        # Handle deletions if enabled
        if options.get("sync_deletes", True):
            synced_fps = set(state.get("requests", {}).keys())
            deleted_fps = synced_fps - set(primary_fingerprints.keys())

            for fp in deleted_fps:
                req_state = state.get("requests", {}).get(fp, {})
                sub_state = req_state.get("subordinates", {}).get(sub_label, {})
                sub_id = sub_state.get("id")

                if sub_id:
                    if _delete_request(sub_url, sub_api_key, sub_id):
                        logger.info(
                            "Seerr sync: Deleted request from %s (id=%d)",
                            sub_label,
                            sub_id,
                        )
                        stats["deleted"] += 1

        # Log summary for this subordinate
        if (
            stats["new"] > 0
            or stats["status_updated"] > 0
            or stats["deleted"] > 0
            or stats["failed"] > 0
        ):
            parts = []
            if stats["new"] > 0:
                parts.append(f"{stats['new']} new")
            if stats["status_updated"] > 0:
                parts.append(f"{stats['status_updated']} status updates")
            if stats["deleted"] > 0:
                parts.append(f"{stats['deleted']} deleted")
            if stats["failed"] > 0:
                parts.append(f"{stats['failed']} failed")
            logger.info(
                "Seerr sync: %s - %s (of %d total requests)",
                sub_label,
                ", ".join(parts),
                total_requests,
            )
        else:
            skipped_msg = ""
            if stats["skipped_failed"] > 0:
                skipped_msg = f", {stats['skipped_failed']} skipped (previously failed)"
            logger.debug(
                "Seerr sync: %s - no changes (%d already synced%s)",
                sub_label,
                stats["already_synced"],
                skipped_msg,
            )

    # Clean up deleted fingerprints from state (do this once after all subordinates)
    if options.get("sync_deletes", True):
        synced_fps = set(state.get("requests", {}).keys())
        deleted_fps = synced_fps - set(primary_fingerprints.keys())
        for fp in deleted_fps:
            state.get("requests", {}).pop(fp, None)

    # Clean up failed entries for requests that no longer exist on primary
    failed_to_remove = []
    for failed_key in state.get("failed", {}).keys():
        # failed_key format is "fingerprint||sub_label"
        fp = failed_key.split("||")[0] if "||" in failed_key else failed_key
        if fp not in primary_fingerprints:
            failed_to_remove.append(failed_key)
    for key in failed_to_remove:
        state.get("failed", {}).pop(key, None)

    # Update last poll timestamp
    state["last_poll_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save_sync_state(state)


def _sync_loop() -> None:
    """Main sync loop that runs continuously."""
    logger.info("Seerr sync service started")

    while True:
        try:
            sync_cfg = CONFIG_MANAGER.get("seerr_sync", {})

            if not sync_cfg.get("enabled"):
                time.sleep(60)
                continue

            interval = sync_cfg.get("poll_interval_seconds", 60)
            interval = max(10, interval)  # Minimum 10 seconds

            run_sync_cycle()

            time.sleep(interval)

        except Exception as e:
            logger.error("Seerr sync error: %s", e)
            time.sleep(60)


def start_seerr_sync_service() -> None:
    """Start the Seerr sync service in a background thread."""
    sync_cfg = CONFIG_MANAGER.get("seerr_sync", {})

    if not sync_cfg.get("enabled"):
        logger.debug("Seerr sync service disabled")
        return

    # Validate configuration
    errors = validate_seerr_sync_config()
    if errors:
        for error in errors:
            logger.error("Seerr sync config error: %s", error)
        logger.error("Seerr sync service not started due to configuration errors")
        return

    thread = threading.Thread(target=_sync_loop, daemon=True, name="seerr-sync")
    thread.start()
    logger.info("Seerr sync service started in background")
