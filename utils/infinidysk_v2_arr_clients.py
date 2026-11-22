"""Preserve existing managed Arr clients across v1's two-key to v2's one-key API.

Only a passing, migration-bound saved-client test authorizes legacy ownership.
No client is created or adopted by its name alone, and no native key is rotated.
"""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import urllib.error
import urllib.request

import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException

from utils.core_services import has_core_service
from utils.url_security import safe_request

_FAMILIES = {"radarr": "v3", "sonarr": "v3", "lidarr": "v1", "whisparr": "v3"}
_ALIASES = {"infinidysk", "nzbdav"}
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_MAX_RESPONSE = 2 * 1024 * 1024
_MESSAGES = {
    "scope_changed": "The retained managed Arr connection scope changed. Review its download client before retrying.",
    "source_unverified": "An existing managed Arr download client did not pass its v1 connection test. Correct that connection before starting the migration.",
    "client_unverified": "The saved Arr download client could not be verified against InfiniDysk 2. Review its connection and retry consumer recovery; the converted library is retained.",
    "client_ambiguous": "A unique existing managed InfiniDysk download client could not be established. Review the Arr connection; DUMB did not create or adopt another client.",
    "proof_missing": "This managed Arr has no verified pre-migration client binding. Review its existing download client before retrying consumer recovery.",
    "native_key_unavailable": "The effective native download-client key could not be read or changed during reconciliation. Review it before retrying consumer recovery.",
    "ownership_unverified": "The saved DUMB ownership record for this Arr connection needs review. Existing client settings were preserved.",
    "arr_unready": "The managed Arr API configuration is not ready yet. DUMB will retry its connection after startup.",
    "native_recovery_pending": "A native InfiniDysk runtime update needs recovery review. Arr credential synchronization is paused to preserve its recorded settings.",
}


class ArrClientReconciliationError(ValueError):
    def __init__(self, code, process_name=None):
        self.reason_code = code if code in _MESSAGES else "client_unverified"
        self.process_name = process_name
        super().__init__(_MESSAGES[self.reason_code])


def _fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _configuration(manager):
    value = getattr(manager, "config", manager)
    if not isinstance(value, dict):
        raise ArrClientReconciliationError("scope_changed")
    return value


def _scopes(configuration, consumers, retained_names):
    config = _configuration(configuration)
    retained = set(retained_names)
    result = []
    for consumer in consumers:
        family, instance = consumer.get("service_key"), consumer.get("instance")
        name = consumer.get("process_name")
        if family not in _FAMILIES or name not in retained:
            continue
        section = config.get(family) or {}
        instances = section.get("instances")
        entry = (
            instances.get(instance)
            if isinstance(instances, dict)
            else section if instance is None else None
        )
        if not isinstance(entry, dict):
            raise ArrClientReconciliationError("scope_changed", name)
        if not has_core_service(entry, "infinidysk"):
            continue
        if (
            entry.get("enabled") is not True
            or entry.get("process_name") != name
            or (
                consumer.get("config_fingerprint")
                and consumer["config_fingerprint"] != _fingerprint(entry)
            )
        ):
            raise ArrClientReconciliationError("scope_changed", name)
        category = str(
            entry.get("instance_name") or entry.get("name") or instance or ""
        ).strip()
        category = "".join(
            char if char.isalnum() or char in "-_" else "-"
            for char in f"{family}-{category}".lower()
        ).strip("-")
        while "--" in category:
            category = category.replace("--", "-")
        result.append(
            {
                "service_key": family,
                "instance": instance,
                "process_name": name,
                "config_fingerprint": _fingerprint(entry),
                "category": category,
                "entry": entry,
                "api_version": _FAMILIES[family],
            }
        )
    if len({item["process_name"] for item in result}) != len(result):
        raise ArrClientReconciliationError("scope_changed")
    return result


def _connection(scope):
    """Read only the known managed XML file, never an arbitrary remote Arr URL."""
    try:
        path = Path(scope["entry"]["config_file"])
        if not path.is_absolute():
            raise ValueError
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError
            raw = handle.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError
            after = os.fstat(handle.fileno())
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError
        root = ET.fromstring(raw)
        key = (root.findtext("ApiKey") or "").strip()
        port = scope["entry"].get("port") or scope["entry"].get("host_port")
        if isinstance(port, bool) or not 1 <= int(port) <= 65535 or not key:
            raise ValueError
        base = (root.findtext("UrlBase") or "").strip()
        if base and (
            not base.startswith("/")
            or base.startswith("//")
            or any(char in base for char in "?#\\\r\n")
        ):
            raise ValueError
        origin = f"http://127.0.0.1:{int(port)}{base.rstrip('/')}"
        return origin, key
    except (
        OSError,
        KeyError,
        ValueError,
        TypeError,
        ET.ParseError,
        DefusedXmlException,
    ):
        raise ArrClientReconciliationError(
            "scope_changed", scope["process_name"]
        ) from None


def _request(origin, key, api_version, path, method="GET", payload=None):
    """Bounded local-only requests; response errors never disclose query keys."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *unused):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    headers = {"X-Api-Key": key, "Accept": "application/json"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    request = safe_request(
        f"{origin}/api/{api_version}/{path}",
        data=body,
        method=method,
        headers=headers,
    )
    try:
        with opener.open(request, timeout=10) as response:
            raw = response.read(_MAX_RESPONSE + 1)
            if len(raw) > _MAX_RESPONSE:
                raise ValueError
            return json.loads(raw) if raw else None
    except (OSError, ValueError, urllib.error.URLError):
        raise ArrClientReconciliationError("client_unverified") from None


def _fields(client):
    result = {}
    for item in client.get("fields") or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ArrClientReconciliationError("client_ambiguous")
        name = item["name"].lower()
        if name in result:
            raise ArrClientReconciliationError("client_ambiguous")
        result[name] = item.get("value")
    return result


def _resource_fingerprint(client):
    if not isinstance(client, dict) or not isinstance(client.get("fields"), list):
        raise ArrClientReconciliationError("scope_changed")
    _fields(client)
    value = copy.deepcopy(client)
    # Secret-only changes are tested by the Arr itself. Keep every other
    # resource byte in the binding, including masked passwords and field order.
    for field in value.get("fields") or []:
        if str(field.get("name") or "").lower() == "apikey":
            field["value"] = "[download-client credential]"
    return _fingerprint(value)


def _client_matches(client, scope, service):
    if (
        not isinstance(client, dict)
        or str(client.get("name") or "").lower() not in _ALIASES
        or str(client.get("implementation") or "").lower() != "sabnzbd"
    ):
        return False
    fields = _fields(client)
    port = fields.get("port")
    expected = service.get("backend_port")
    return (
        type(client.get("id")) is int
        and client["id"] > 0
        and client.get("enable") is True
        and str(client.get("protocol") or "").lower() == "usenet"
        and fields.get("host") in _LOOPBACK
        and type(port) is int
        and type(expected) is int
        and 1 <= expected <= 65535
        and port == expected
        and fields.get("usessl") is False
        and not fields.get("urlbase")
        and any(
            fields.get(name) == scope["category"]
            for name in ("category", "moviecategory", "tvcategory", "musiccategory")
        )
        and "apikey" in fields
    )


def _call(request, connection, scope, path, method="GET", payload=None):
    try:
        return request(*connection, scope["api_version"], path, method, payload)
    except ArrClientReconciliationError as error:
        raise ArrClientReconciliationError(
            error.reason_code, scope["process_name"]
        ) from None
    except Exception:
        raise ArrClientReconciliationError(
            "client_unverified", scope["process_name"]
        ) from None


def _passes(request, connection, scope, client):
    try:
        result = _call(
            request, connection, scope, "downloadclient/test", "POST", client
        )
        return result in (None, {}, [])
    except ArrClientReconciliationError:
        return False


def capture_arr_bindings(
    configuration,
    source_service,
    consumers,
    retained_names,
    *,
    plan_fingerprint=None,
    request=None,
    check_cancel=None,
):
    """Qualify exact existing legacy clients while v1 and its Arrs still run."""
    request, check_cancel = request or _request, check_cancel or (lambda: None)
    records = []
    for scope in _scopes(configuration, consumers, retained_names):
        check_cancel()
        connection = _connection(scope)
        clients = _call(request, connection, scope, "downloadclient")
        if not isinstance(clients, list):
            raise ArrClientReconciliationError(
                "client_ambiguous", scope["process_name"]
            )
        matches = [
            item for item in clients if _client_matches(item, scope, source_service)
        ]
        if len(matches) != 1:
            raise ArrClientReconciliationError(
                "client_ambiguous", scope["process_name"]
            )
        client = matches[0]
        if not _passes(request, connection, scope, client):
            raise ArrClientReconciliationError(
                "source_unverified", scope["process_name"]
            )
        check_cancel()
        current = _call(request, connection, scope, f"downloadclient/{client['id']}")
        if _resource_fingerprint(current) != _resource_fingerprint(client):
            raise ArrClientReconciliationError("scope_changed", scope["process_name"])
        records.append(
            {
                key: scope[key]
                for key in (
                    "service_key",
                    "instance",
                    "process_name",
                    "config_fingerprint",
                    "category",
                    "api_version",
                )
            }
            | {
                "connection_fingerprint": _fingerprint(connection),
                "client_id": client["id"],
                "resource_fingerprint": _resource_fingerprint(client),
                "source_test_verified": True,
            }
        )
    return {"schema": 1, "plan_fingerprint": plan_fingerprint, "records": records}


def _effective_native_key(service, data_root):
    from utils.infinidysk_v2_runtime import native_api_request

    response = native_api_request(
        service, "GET", "/api/v1/settings/keys/sab.api_key", data_root=data_root
    )
    if response.get("status") != 200:
        raise ArrClientReconciliationError("native_key_unavailable")
    return (response.get("body") or {}).get("value")


def reconcile_saved_client(
    request, connection, scope, service, record, key, check_cancel
):
    """Update only the credential of one exact, previously qualified resource."""
    identifier = record.get("client_id")
    if type(identifier) is not int or identifier <= 0:
        raise ArrClientReconciliationError("proof_missing", scope["process_name"])
    path = f"downloadclient/{identifier}"
    original = _call(request, connection, scope, path)
    if (
        not _client_matches(original, scope, service)
        or original.get("id") != identifier
        or _resource_fingerprint(original) != record.get("resource_fingerprint")
    ):
        raise ArrClientReconciliationError("scope_changed", scope["process_name"])
    changed = not _passes(request, connection, scope, original)
    if changed:
        desired = copy.deepcopy(original)
        for field in desired["fields"]:
            if field["name"].lower() == "apikey":
                field["value"] = key
        if not _passes(request, connection, scope, desired):
            raise ArrClientReconciliationError(
                "client_unverified", scope["process_name"]
            )
        current = _call(request, connection, scope, path)
        if _resource_fingerprint(current) != record["resource_fingerprint"]:
            raise ArrClientReconciliationError("scope_changed", scope["process_name"])
        check_cancel()
        _call(request, connection, scope, path, "PUT", desired)
    saved = _call(request, connection, scope, path)
    if (
        not isinstance(saved, dict)
        or saved.get("id") != identifier
        or _resource_fingerprint(saved) != record["resource_fingerprint"]
        or not _passes(request, connection, scope, saved)
    ):
        raise ArrClientReconciliationError("client_unverified", scope["process_name"])
    return {
        "process_name": scope["process_name"],
        "client_id": identifier,
        "changed": changed,
        "connection_verified": True,
    }


def reconcile_arr_bindings(
    configuration,
    source_service,
    consumers,
    retained_names,
    proof,
    *,
    plan_fingerprint=None,
    request=None,
    native_key=None,
    check_cancel=None,
    record_ownership=None,
):
    """Repair only API keys of proven saved clients; retries never repeat import."""
    scopes = _scopes(configuration, consumers, retained_names)
    if not scopes:
        return []
    if (
        not isinstance(proof, dict)
        or proof.get("schema") != 1
        or not plan_fingerprint
        or proof.get("plan_fingerprint") != plan_fingerprint
        or not isinstance(proof.get("records"), list)
    ):
        raise ArrClientReconciliationError("proof_missing")
    request, check_cancel = request or _request, check_cancel or (lambda: None)
    config = _configuration(configuration)
    service = config.get("infinidysk") or {}
    try:
        key = (native_key or _effective_native_key)(
            service, config.get("data_root") or "/data"
        )
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= 8192
            or any(ord(char) < 32 or ord(char) == 127 for char in key)
        ):
            raise ValueError
    except Exception:
        raise ArrClientReconciliationError("native_key_unavailable") from None
    results = []
    for scope in scopes:
        check_cancel()
        records = [
            row
            for row in proof["records"]
            if isinstance(row, dict)
            and row.get("process_name") == scope["process_name"]
        ]
        if len(records) != 1:
            raise ArrClientReconciliationError("proof_missing", scope["process_name"])
        record, connection = records[0], _connection(scope)
        if (
            any(
                record.get(name) != scope[name]
                for name in (
                    "service_key",
                    "instance",
                    "config_fingerprint",
                    "category",
                    "api_version",
                )
            )
            or record.get("connection_fingerprint") != _fingerprint(connection)
            or record.get("source_test_verified") is not True
        ):
            raise ArrClientReconciliationError("scope_changed", scope["process_name"])
        result = reconcile_saved_client(
            request, connection, scope, source_service, record, key, check_cancel
        )
        if record_ownership is None:
            from utils.infinidysk_v2_arr_sync import record_managed_binding

            record_managed_binding(scope, connection, record, key)
        else:
            record_ownership(scope, connection, record, key)
        results.append(result)
    return results
