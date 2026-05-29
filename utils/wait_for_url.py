from urllib.parse import urlparse
import requests, time


def _looks_like_webdav_wait(wait_url):
    try:
        path = urlparse(str(wait_url or "")).path.lower()
    except Exception:
        return False
    return (
        path.startswith("/webdav")
        or "/webdav/" in path
        or path.startswith("/dav")
        or "/dav/" in path
    )


def _resolve_probe(wait_entry):
    method = str(wait_entry.get("probe_method") or "").strip().upper()
    headers = dict(wait_entry.get("probe_headers") or {})

    if not method:
        if _looks_like_webdav_wait(wait_entry.get("url")):
            method = "PROPFIND"
            headers.setdefault("Depth", "0")
        else:
            method = "GET"

    return method, headers


def _json_path_exists(payload, path):
    current = payload
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        return False
    return True


def _response_is_ready(response, method, wait_entry=None, logger=None, wait_url=None):
    status_code = response.status_code
    status_ready = 200 <= status_code < 300 or (method == "PROPFIND" and status_code == 207)
    if not status_ready:
        return False

    expected_json_path = (wait_entry or {}).get("expected_json_path")
    if expected_json_path:
        try:
            payload = response.json()
        except ValueError:
            if logger:
                logger.debug(
                    "Response from %s was %s but was not valid JSON while waiting for %s.",
                    wait_url,
                    status_code,
                    expected_json_path,
                )
            return False
        if not _json_path_exists(payload, expected_json_path):
            if logger:
                logger.debug(
                    "Response from %s was %s but missing JSON path %s.",
                    wait_url,
                    status_code,
                    expected_json_path,
                )
            return False

    return True


def wait_for_urls(wait_entries, process_name, logger, shutdown_requested):
    start_time = time.time()

    for wait_entry in wait_entries:
        wait_url = wait_entry.get("url")
        if not wait_url:
            continue
        auth = wait_entry.get("auth")
        method, headers = _resolve_probe(wait_entry)

        logger.info(
            "Waiting to start %s until %s is accessible.",
            process_name,
            wait_url,
        )

        sleep_s = 5
        while time.time() - start_time < 600:
            if shutdown_requested():
                logger.info("Shutdown requested; skipping wait for %s.", wait_url)
                return False, "Shutdown requested"
            try:
                if auth:
                    response = requests.request(
                        method,
                        wait_url,
                        auth=(auth["user"], auth["password"]),
                        headers=headers,
                    )
                else:
                    response = requests.request(method, wait_url, headers=headers)

                if _response_is_ready(response, method, wait_entry, logger, wait_url):
                    expected_json_path = wait_entry.get("expected_json_path")
                    if expected_json_path:
                        logger.info(
                            "%s is accessible with %s via %s and contains JSON path %s.",
                            wait_url,
                            response.status_code,
                            method,
                            expected_json_path,
                        )
                    else:
                        logger.info(
                            "%s is accessible with %s via %s.",
                            wait_url,
                            response.status_code,
                            method,
                        )
                    break

                logger.debug(
                    "Received status code %s from %s %s while waiting for %s.",
                    response.status_code,
                    method,
                    wait_url,
                    wait_url,
                )
            except requests.RequestException as e:
                logger.debug("Waiting for %s via %s: %s", wait_url, method, e)
            time.sleep(sleep_s)
            sleep_s = min(60, int(sleep_s * 1.5))
        else:
            raise RuntimeError(
                f"Timeout: {wait_url} is not accessible after 600 seconds."
            )

    return True, None
