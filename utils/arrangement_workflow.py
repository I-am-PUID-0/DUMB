"""Opt-in, journaled download/import/playback probes with owned cleanup.

Native contracts: SABnzbd documented addfile/queue/history API; Arr v3/v1
automatic import/history APIs; Plex PMS library/parts API; Jellyfin/Emby library media
updates and static streams. These are real operations and never run as ordinary
setup readiness, background retry, or a whole-arrangement recheck.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import posixpath
import re
import signal
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, quote

import defusedxml.ElementTree as ET

from utils.arrangement_http import ApiClient, ApiError

MAX_CONTENT = 8 * 1024 * 1024


class ProbeError(ValueError):
    pass


def schema_fields(resource, config, payload=None):
    if resource["service_key"] not in {"radarr", "sonarr", "lidarr", "whisparr"}:
        return []
    rid = resource["id"]
    return [
        {
            "key": f"{rid}::workflow_probe",
            "resource_id": rid,
            "service_key": resource["service_key"],
            "type": "object",
            "label": "Optional complete workflow test",
            "description": "A real, deliberately small test of provider transfer, Arr import and media decoding. Choose a test item with no existing file and content you are authorized to use. Preview does not enqueue content. Normal setup and automatic retries never run this test.",
            "fields": [
                {
                    "key": "provider",
                    "type": "select",
                    "label": "Download provider",
                    "options": [
                        {"value": key, "label": key}
                        for key in ("infinidysk", "decypharr", "altmount")
                        if (config.get(key) or {}).get("enabled")
                    ],
                },
                {
                    "key": "media_server",
                    "type": "select",
                    "label": "Playback server",
                    "options": [
                        {"value": key, "label": key}
                        for key in ("plex", "jellyfin", "emby", "audiobookshelf")
                        if (config.get(key) or {}).get("enabled")
                    ],
                },
                {
                    "key": "source_url",
                    "type": "password",
                    "secret": True,
                    "label": "Authorized small test NZB URL",
                },
                {
                    "key": "allow_private_source",
                    "type": "boolean",
                    "label": "Authorize a private-network source URL",
                },
                {
                    "key": "item_id",
                    "type": "number",
                    "min": 1,
                    "max": 2147483647,
                    "label": "Existing fileless movie / episode / album ID",
                },
                {
                    "key": "download_client_id",
                    "type": "number",
                    "min": 0,
                    "max": 2147483647,
                    "default": 0,
                    "label": "Native download client ID (0 chooses a unique matching client)",
                },
                {
                    "key": "max_download_bytes",
                    "type": "number",
                    "min": 1024,
                    "max": MAX_CONTENT,
                    "default": 1048576,
                    "label": "Maximum declared content bytes",
                },
                {
                    "key": "timeout_seconds",
                    "type": "number",
                    "min": 30,
                    "max": 300,
                    "default": 120,
                    "label": "Whole-test time limit (seconds)",
                },
                {
                    "key": "authorize_download_import_scan",
                    "type": "boolean",
                    "default": False,
                    "label": "I authorize this test download, import, exact-item refresh and playback",
                },
                {
                    "key": "authorize_reserved_test_item",
                    "type": "boolean",
                    "default": False,
                    "label": "I reserved this unmonitored fileless item and will leave it unchanged until the test finishes",
                },
                {
                    "key": "release_name",
                    "type": "string",
                    "label": "Native release title (optional)",
                    "description": "Used only if the derived title cannot be recognized. The native parser must match this exact selected item before anything is enqueued.",
                },
                {
                    "key": "authorize_server_scan",
                    "type": "boolean",
                    "default": False,
                    "label": "Authorize Jellyfin scanning all configured libraries when its API requires it",
                },
                {
                    "key": "authorize_library_scan",
                    "type": "boolean",
                    "default": False,
                    "label": "Authorize scanning the containing library when needed (required for Audiobookshelf)",
                },
                {
                    "key": "cleanup_after",
                    "type": "boolean",
                    "default": True,
                    "label": "Remove only this test's created file and provider job afterward",
                },
            ],
        },
        {
            "key": f"{rid}::workflow_preview_id",
            "resource_id": rid,
            "service_key": resource["service_key"],
            "type": "string",
            "internal": True,
            "label": "Accepted workflow preview",
        },
        {
            "key": f"{rid}::workflow_cleanup",
            "resource_id": rid,
            "service_key": resource["service_key"],
            "type": "boolean",
            "default": False,
            "label": "Recover and clean up the previous test only",
            "description": "Only the journaled job and unchanged imported test file are eligible; this never starts another download or import.",
        },
    ]


def _result(execution, reason, verification="not_checked", evidence=None):
    return {
        "execution": execution,
        "verification": verification,
        "reason": reason,
        "retryable": False,
        "evidence": evidence or {},
    }


def _get(client, path, **kwargs):
    response = client.request("GET", path, **kwargs)
    response.raise_for_status()
    return response.json()


def _request(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else {}


def _digest(value):
    # Private workflow replay/cleanup bindings, never login password verification.
    # Keep existing digest bytes so unfinished journals retain their exact scope.
    # codeql[py/weak-sensitive-data-hashing]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _scope_options(options):
    return {
        key: value
        for key, value in options.items()
        if key
        not in {
            "authorize_download_import_scan",
            "authorize_library_scan",
            "authorize_reserved_test_item",
            "authorize_server_scan",
        }
    }


def _native_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ProbeError("Wait for a regular native provider configuration.")
        raw = handle.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ProbeError(
            "The native provider configuration exceeds its supported bound."
        )
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ProbeError("The native provider configuration uses an unsupported shape.")
    return value


def _provider_access(provider, cfg):
    env = cfg.get("env") or {}
    base = ""
    token = env.get(
        "FRONTEND_BACKEND_API_KEY" if provider == "infinidysk" else "ALTMOUNT_API_KEY"
    ) or cfg.get("api_key")
    if provider == "decypharr":
        directory = cfg.get("config_dir") or "/decypharr"
        try:
            token = _native_json(os.path.join(directory, "auth.json")).get("api_token")
        except FileNotFoundError:
            pass
        path = cfg.get("config_file") or os.path.join(directory, "config.json")
        base = str(_native_json(path).get("url_base") or "").rstrip("/")
    if provider in {"decypharr", "altmount"}:
        base += "/sabnzbd"
    if base and (
        not base.startswith("/")
        or base.startswith("//")
        or any(c in base for c in "?#\\\r\n")
    ):
        raise ProbeError("The managed provider uses an unsupported native base path.")
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 4096
        or "*" in token
        or any(c in token for c in "\r\n\0")
    ):
        raise ProbeError(
            "Wait for the selected provider's protected native API credential. Masked Arr credentials are never used for a workflow test."
        )
    return token, base


class NativeWorkflow:
    """Narrow native operations; never search/grab or replace an existing file."""

    def __init__(self, action, config, payload, options, factory):
        self.action, self.config, self.payload, self.options, self.factory = (
            action,
            config,
            payload,
            options,
            factory,
        )
        self.arr = self.provider = self.media = None
        self.remote_mappings = []
        self.family = action["service_key"]
        self.prefix = "/api/v1" if self.family == "lidarr" else "/api/v3"

    def connect(self, write=False):
        from utils.arrangement_plan import resource_config
        from utils.arrangement_media import _client as media_client

        if self.action["source"].startswith("external_arr."):
            from utils.arrangement_external import resolve_target

            target = resolve_target(
                self.payload,
                self.action["source"],
                client_factory=self.factory,
                require_write=write,
            )
            self.arr, self.prefix = target["client"], target["api_prefix"]
        elif self.arr is None:
            cfg = resource_config(self.config, self.action["source"])
            path = cfg.get("config_file")
            if not path or os.path.islink(path):
                raise ProbeError(
                    "Wait for the selected Arr's regular generated configuration."
                )
            document = ET.parse(path).getroot()
            key, base = document.findtext("ApiKey") or "", (
                document.findtext("UrlBase") or ""
            ).rstrip("/")
            if not key or (
                base
                and (
                    not base.startswith("/")
                    or base.startswith("//")
                    or any(c in base for c in "?#\\\r\n")
                )
            ):
                raise ProbeError(
                    "The selected Arr credentials or URL base are not ready."
                )
            self.arr = self.factory(
                f"http://127.0.0.1:{int(cfg['port'])}{base}",
                headers={"X-Api-Key": key},
                managed=True,
            )
        provider = self.options["provider"]
        cfg = self.config.get(provider) or {}
        if not cfg.get("enabled"):
            raise ProbeError("Start the selected provider before testing.")
        expected_port = int(
            cfg.get("backend_port", 8080)
            if provider == "infinidysk"
            else cfg.get("port", 8088 if provider == "altmount" else 8282)
        )
        self.provider_key, native_base = _provider_access(provider, cfg)
        external = self.action["source"].startswith("external_arr.")
        expected = None
        if external:
            values = (self.payload.get("arrangement") or {}).get("values") or {}
            expected = urlsplit(
                values.get(f"{self.action['source']}::{provider}::client_url") or ""
            )
            if expected.scheme not in {"http", "https"} or not expected.hostname:
                raise ProbeError(
                    "Configure this external Arr's explicit provider endpoint before testing."
                )
        expected_base = native_base
        if expected is not None:
            expected_base = expected.path.rstrip("/")
            suffix = "/sabnzbd" if provider in {"altmount", "decypharr"} else ""
            if suffix and not expected_base.endswith(suffix):
                expected_base += suffix
        clients = _get(self.arr, self.prefix + "/downloadclient")
        candidates = []
        for client in clients if isinstance(clients, list) else []:
            fields = {
                str(field.get("name", "")).lower(): field.get("value")
                for field in client.get("fields") or []
                if isinstance(field, dict)
            }
            if (
                client.get("enable") is True
                and str(client.get("implementation", "")).lower() == "sabnzbd"
                and str(fields.get("port"))
                == str(
                    expected.port or (443 if expected.scheme == "https" else 80)
                    if expected
                    else expected_port
                )
                and str(fields.get("host", "")).lower()
                in (
                    {expected.hostname.lower()}
                    if expected
                    else {"localhost", "127.0.0.1", "::1"}
                )
                and bool(fields.get("usessl"))
                == (expected.scheme == "https" if expected else False)
                and str(fields.get("urlbase") or "").rstrip("/") == expected_base
                and (
                    not self.options.get("download_client_id")
                    or client.get("id") == self.options["download_client_id"]
                )
            ):
                category = (
                    fields.get("moviecategory")
                    or fields.get("tvcategory")
                    or fields.get("musiccategory")
                    or fields.get("category")
                )
                if category and fields.get("apikey"):
                    candidates.append((client, fields, category))
        if len(candidates) != 1:
            raise ProbeError(
                "Configure exactly one enabled SAB-compatible client for this selected provider and Arr category before testing."
            )
        self.client_record, fields, self.category = candidates[0]
        self.provider = self.factory(
            f"http://127.0.0.1:{expected_port}{native_base}", managed=True
        )
        self.client_endpoint = {
            key: fields.get(key) for key in ("host", "port", "urlbase", "usessl")
        }
        mappings = _get(self.arr, self.prefix + "/remotepathmapping")
        if not isinstance(mappings, list):
            raise ProbeError(
                "The Arr returned an unsupported remote-path mapping contract."
            )
        self.remote_mappings = [
            mapping
            for mapping in mappings
            if str(mapping.get("host") or "").lower()
            == str(fields.get("host") or "").lower()
        ]
        media = self.options["media_server"]
        if not (self.config.get(media) or {}).get("enabled"):
            raise ProbeError(
                "Start and authorize the selected playback server before testing."
            )
        self.media = media_client(media, media, self.config, self.payload, self.factory)

    def item(self):
        kind = (
            "episode"
            if self.family == "sonarr"
            else "album" if self.family == "lidarr" else "movie"
        )
        return _get(self.arr, f"{self.prefix}/{kind}/{self.options['item_id']}")

    def files(self, item):
        if self.family in {"radarr", "whisparr"}:
            file_id = item.get("movieFileId") or (item.get("movieFile") or {}).get("id")
            return (
                [_get(self.arr, f"{self.prefix}/moviefile/{file_id}")]
                if file_id
                else []
            )
        if self.family == "sonarr":
            file_id = item.get("episodeFileId")
            return (
                [_get(self.arr, f"{self.prefix}/episodefile/{file_id}")]
                if file_id
                else []
            )
        tracks = _get(
            self.arr,
            self.prefix + "/track",
            params={"albumId": self.options["item_id"]},
        )
        identifiers = sorted(
            {
                row.get("trackFileId")
                for row in tracks
                if isinstance(row, dict) and row.get("trackFileId")
            }
        )
        return [
            _get(self.arr, f"{self.prefix}/trackfile/{identifier}")
            for identifier in identifiers
        ]

    def binding(self):
        return {
            "arr_url": self.arr.base_url,
            "provider_url": self.provider.base_url,
            "media_url": self.media.base_url,
            "source": self.action["source"],
            "provider": self.options["provider"],
            "media_server": self.options["media_server"],
            "item_id": self.options["item_id"],
            "client_id": self.client_record.get("id"),
            "category": self.category,
            "client_endpoint": self.client_endpoint,
            "provider_credential": _digest(self.provider_key),
            "arr_auth": _digest(getattr(self.arr, "headers", {})),
            "media_auth": _digest(getattr(self.media, "headers", {})),
            "remote_mappings": _digest(self.remote_mappings),
        }

    def prepare(self, write=False, marker="DUMB.Setup.Probe"):
        self.connect(write)
        status = _get(self.arr, self.prefix + "/system/status")
        if (
            not isinstance(status, dict)
            or str(status.get("appName", "")).lower() != self.family
        ):
            raise ProbeError(
                "The selected Arr did not report the expected native identity."
            )
        item = self.item()
        if (
            not isinstance(item, dict)
            or item.get("id") != self.options["item_id"]
            or self.files(item)
            or item.get("hasFile") is True
        ):
            raise ProbeError(
                "Choose an existing test item with no media file. Existing files are never replaced by a probe."
            )
        if item.get("monitored") is not False:
            raise ProbeError(
                "Reserve an unmonitored fileless test item before running a real workflow test."
            )
        queue = _get(
            self.arr, self.prefix + "/queue", params={"page": 1, "pageSize": 1000}
        )
        if (
            not isinstance(queue, dict)
            or not isinstance(queue.get("records"), list)
            or type(queue.get("totalRecords")) is not int
            or not 0 <= queue["totalRecords"] <= 1000
            or len(queue["records"]) != queue["totalRecords"]
            or any(not isinstance(row, dict) for row in queue["records"])
        ):
            raise ProbeError(
                "The native download queue cannot be safely checked within this test's bound."
            )
        identity = (
            "episodeId"
            if self.family == "sonarr"
            else "albumId" if self.family == "lidarr" else "movieId"
        )
        if any(row.get(identity) == item["id"] for row in queue["records"]):
            raise ProbeError(
                "Another download already targets this test item. Let it finish before preparing a test."
            )
        handling = _get(self.arr, self.prefix + "/config/downloadclient")
        if (
            not isinstance(handling, dict)
            or handling.get("enableCompletedDownloadHandling") is not True
        ):
            raise ProbeError(
                "Enable native completed-download handling before testing automatic import. The probe never changes this policy."
            )
        findings = _request(
            self.arr,
            "POST",
            self.prefix + "/downloadclient/test",
            json=self.client_record,
        )
        if findings not in ({}, [], "{}"):
            raise ProbeError(
                "The selected Arr's native download-client connection test did not pass."
            )
        if self.family == "sonarr":
            parent = _get(self.arr, f"{self.prefix}/series/{item['seriesId']}")
        elif self.family == "lidarr":
            parent = _get(self.arr, f"{self.prefix}/artist/{item['artistId']}")
        else:
            parent = item
        if not isinstance(parent.get("path"), str) or not parent["path"].startswith(
            "/"
        ):
            raise ProbeError(
                "The selected test item needs a configured absolute library path."
            )
        media = self.options["media_server"]
        identity = _get(
            self.media,
            (
                "/identity"
                if media == "plex"
                else "/api/me" if media == "audiobookshelf" else "/System/Info"
            ),
        )
        if not isinstance(identity, dict):
            raise ProbeError("Authorize the playback server before testing.")
        scan_scope = None
        if (
            self.options["media_server"] in {"jellyfin", "emby"}
            and self.options.get("authorize_library_scan") is True
        ):
            scan_scope = self._scan_scope(parent["path"])
            if (
                write
                and scan_scope["library_count"] > 1
                and self.options.get("authorize_server_scan") is not True
            ):
                raise ProbeError(
                    "The native scanner refreshes every configured library. Explicitly authorize that additional scope before anything is enqueued."
                )
        self.sab("queue", limit=1)
        from utils.arrangement_workflow_target import (
            default_release_name,
            validate_target,
        )

        release = default_release_name(
            item,
            parent,
            self.family,
            marker,
            release_name=self.options.get("release_name"),
        )
        validate_target(self.arr, self.prefix, self.family, release, item["id"])
        return {
            "binding": self.binding(),
            "item_id": item["id"],
            "path": parent["path"],
            "title": str(item.get("title") or parent.get("title") or ""),
            "files": [],
            "job_name": release,
            "scan_scope": scan_scope,
        }

    def sab(self, mode, **params):
        value = _get(
            self.provider,
            "/api",
            params={
                "mode": mode,
                "apikey": self.provider_key,
                "output": "json",
                **params,
            },
        )
        if isinstance(value, dict) and (
            value.get("status") is False or value.get("error")
        ):
            raise ProbeError(
                "The provider rejected its authenticated workflow operation."
            )
        return value

    def enqueue(self, manifest, marker):
        boundary = "DUMBProbe" + uuid.uuid4().hex
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="name"; filename="{marker}.nzb"\r\nContent-Type: application/x-nzb\r\n\r\n'
            ).encode()
            + manifest
            + f"\r\n--{boundary}--\r\n".encode()
        )
        value = _request(
            self.provider,
            "POST",
            "/api",
            params={
                "mode": "addfile",
                "apikey": self.provider_key,
                "output": "json",
                "cat": self.category,
                "nzbname": marker,
                "pp": 0,
                "priority": 0,
            },
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        ids = value.get("nzo_ids") if isinstance(value, dict) else None
        if not isinstance(ids, list) or len(ids) != 1:
            raise ProbeError(
                "The provider did not identify one test job. Its journal must be recovered before another enqueue."
            )
        return self.job_id(ids[0])

    @staticmethod
    def job_id(value):
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value)
            or value.lower() in {"all", "failed", "completed"}
        ):
            raise ProbeError("The provider returned an unsafe job identity.")
        return value

    def _jobs(self, marker):
        result = []
        for mode in ("queue", "history"):
            data = self.sab(mode, search=marker, limit=100)
            rows = data.get(mode, {}).get("slots", []) if isinstance(data, dict) else []
            result.extend(
                (mode, row)
                for row in rows
                if isinstance(row, dict)
                and str(
                    row.get("name") or row.get("nzb_name") or row.get("filename") or ""
                ).removesuffix(".nzb")
                == marker
            )
        return result

    def recover_job(self, marker):
        self.connect(write=True)
        ids = {self.job_id(row.get("nzo_id")) for _, row in self._jobs(marker)}
        if len(ids) != 1:
            raise ProbeError(
                "The ambiguous enqueue cannot be safely recovered. Inspect its unique test marker before retrying."
            )
        return ids.pop()

    def downloaded(self, record):
        for mode, row in self._jobs(record["marker"]):
            if self.job_id(row.get("nzo_id")) != record["job_id"]:
                continue
            reported = row.get("bytes")
            if reported is not None:
                if isinstance(reported, bool) or not str(reported).isdigit():
                    raise ProbeError(
                        "The provider reported an unrecognized content size."
                    )
                if int(reported) > self.options["max_download_bytes"]:
                    raise ProbeError(
                        "The provider reported content exceeding the authorized byte budget."
                    )
            if mode == "history":
                if str(row.get("status", "")).lower() == "failed":
                    raise ProbeError("The selected provider test job failed.")
                if str(row.get("status", "")).lower() == "completed":
                    path = row.get("storage") or row.get("path")
                    if (
                        not isinstance(path, str)
                        or not path.startswith("/")
                        or ".." in path.split("/")
                    ):
                        raise ProbeError(
                            "The completed test job did not report a safe storage path."
                        )
                    return {"path": path, "job_id": record["job_id"]}
        return None

    def begin_import(self, record, checkpoint):
        """Observe native completion; never force an import or bypass policy."""
        if record.get("import_intent"):
            return
        if self.files(self.item()):
            try:
                automatic = self.imported(record)
            except ProbeError:
                automatic = None
            if automatic is None:
                raise ProbeError(
                    "A file appeared without matching native provenance. It is preserved; the test will not force an import."
                )
            record["imported"] = automatic
        record["import_folder"] = self.import_folder(record["download"]["path"])
        record["import_intent"] = {"automatic": True, "download_id": record["job_id"]}
        checkpoint()

    def import_folder(self, folder):
        matches = []
        for mapping in self.remote_mappings:
            remote, local = mapping.get("remotePath"), mapping.get("localPath")
            if not all(
                isinstance(value, str)
                and value.startswith("/")
                and ".." not in value.split("/")
                for value in (remote, local)
            ):
                raise ProbeError(
                    "The selected client has an unsupported remote-path mapping."
                )
            remote = remote.rstrip("/") or "/"
            if folder == remote or folder.startswith(remote.rstrip("/") + "/"):
                matches.append(
                    (
                        len(remote),
                        posixpath.join(local, folder[len(remote) :].lstrip("/")),
                    )
                )
        if not matches:
            return folder
        longest = max(length for length, _ in matches)
        candidates = {path for length, path in matches if length == longest}
        if len(candidates) != 1:
            raise ProbeError(
                "Several native mappings disagree about this exact completed job path."
            )
        return candidates.pop()

    def imported(self, record):
        files = self.files(self.item())
        if not files:
            if record.get("command_id"):
                status = _get(self.arr, f"{self.prefix}/command/{record['command_id']}")
                if str(status.get("status", "")).lower() in {
                    "failed",
                    "aborted",
                    "cancelled",
                }:
                    raise ProbeError("The native Arr import command failed.")
            return None
        if len(files) != 1:
            raise ProbeError("The probe expected exactly one new native media file.")
        row = files[0]
        root = record["baseline"]["path"].rstrip("/")
        if (
            not isinstance(row.get("id"), int)
            or not isinstance(row.get("path"), str)
            or not row["path"].startswith(root + "/")
            or not 0 < int(row.get("size") or 0) <= self.options["max_download_bytes"]
        ):
            raise ProbeError(
                "The imported file identity, path or size differs from the previewed test item."
            )
        history = _get(
            self.arr,
            self.prefix + "/history",
            params={
                "page": 1,
                "pageSize": 100,
                "sortKey": "date",
                "sortDirection": "descending",
            },
        )
        events = history.get("records", []) if isinstance(history, dict) else []

        def proves_import(event):
            if not isinstance(event, dict):
                return False
            data = event.get("data") or {}
            if data.get("importedPath") != row["path"]:
                return False
            if event.get("downloadId") == record["job_id"]:
                return True
            intent = record.get("import_intent") or {}
            source = intent.get("path") or ""
            # Folder-only native manual imports can omit downloadId. Accept
            # only exact native provenance from this run's unique job folder,
            # including native file ID and size, rather than timing/title.
            return (
                event.get("downloadId") in (None, "")
                and record.get("marker") in source.split("/")
                and data.get("droppedPath") == source
                and str(data.get("fileId")) == str(row["id"])
                and str(data.get("size")) == str(row["size"])
            )

        owned = any(proves_import(event) for event in events)
        if not owned:
            # Native import history, rather than timing alone, establishes
            # ownership when another client can also import this item.
            return None
        return {
            "id": row["id"],
            "path": row["path"],
            "size": row["size"],
            "fingerprint": _digest(
                {key: row.get(key) for key in ("id", "path", "size", "dateAdded")}
            ),
        }

    def refresh_media(self, record, *, removed=False):
        service, path = self.options["media_server"], record["imported"]["path"]
        if service in {"jellyfin", "emby"}:
            _request(
                self.media,
                "POST",
                "/Library/Media/Updated",
                json={
                    "Updates": [
                        {
                            "Path": path,
                            "UpdateType": "Deleted" if removed else "Created",
                        }
                    ]
                },
            )
            if self.options.get("authorize_library_scan") is True:
                self._refresh_containing_library(path, record)
        elif service == "plex":
            libraries = (
                _get(self.media, "/library/sections")
                .get("MediaContainer", {})
                .get("Directory", [])
            )
            matches = [
                row
                for row in libraries
                if any(
                    path.startswith(str(location.get("path") or "").rstrip("/") + "/")
                    for location in row.get("Location") or []
                    if location.get("path")
                )
            ]
            if len(matches) != 1:
                raise ProbeError(
                    "Choose a library containing the exact imported test path."
                )
            record["media_library_id"] = str(matches[0]["key"])
            self.media.request(
                "GET",
                f"/library/sections/{quote(record['media_library_id'], safe='')}/refresh",
                params={"path": os.path.dirname(path)},
            ).raise_for_status()
        else:
            if self.options.get("authorize_library_scan") is not True:
                raise ProbeError(
                    "Audiobookshelf requires explicit consent for its containing-library scan."
                )
            libraries = _get(self.media, "/api/libraries").get("libraries", [])
            matches = [
                row
                for row in libraries
                if any(
                    path.startswith(str(folder.get("fullPath") or "").rstrip("/") + "/")
                    for folder in row.get("folders") or []
                    if folder.get("fullPath")
                )
            ]
            if len(matches) != 1:
                raise ProbeError(
                    "Choose an Audiobookshelf test library containing the imported path."
                )
            record["media_library_id"] = matches[0]["id"]
            _request(
                self.media,
                "POST",
                f"/api/libraries/{quote(record['media_library_id'], safe='')}/scan",
            )

    def _scan_scope(self, path):
        service = self.options["media_server"]
        folders = _get(
            self.media,
            (
                "/Library/VirtualFolders"
                if service == "jellyfin"
                else "/Library/VirtualFolders/Query"
            ),
        )
        if isinstance(folders, dict):
            folders = folders.get("Items", folders.get("items", []))
        matches = (
            [
                folder
                for folder in folders
                if any(
                    path.startswith(str(root).rstrip("/") + "/")
                    for root in (
                        folder.get("Locations") or folder.get("locations") or []
                    )
                    if root
                )
            ]
            if isinstance(folders, list)
            else []
        )
        if len(matches) != 1:
            raise ProbeError(
                "Choose one containing media library before authorizing its initialization scan."
            )
        return {
            "library_count": len(folders),
            "libraries": [
                {
                    "id": folder.get("ItemId") or folder.get("itemId"),
                    "paths": folder.get("Locations") or folder.get("locations") or [],
                }
                for folder in folders
            ],
        }

    def _refresh_containing_library(self, path, record):
        scope = self._scan_scope(path)
        if scope != record["baseline"].get("scan_scope"):
            raise ProbeError(
                "The media library scan scope changed after preview. The broader scan was refused."
            )
        if (
            scope["library_count"] != 1
            and self.options.get("authorize_server_scan") is not True
        ):
            raise ProbeError(
                "This server's supported scan API refreshes all configured libraries. Explicitly authorize that additional scope before retrying."
            )
        _request(self.media, "POST", "/Library/Refresh")

    def _playback_endpoint(self, record):
        service, imported = self.options["media_server"], record["imported"]
        if service in {"jellyfin", "emby"}:
            result = _get(
                self.media,
                "/Items",
                params={
                    "Recursive": "true",
                    "SearchTerm": record["baseline"]["title"],
                    "Fields": "Path,MediaSources",
                    "Limit": 100,
                },
            )
            rows = result.get("Items", result.get("items", []))
            matches = [
                row
                for row in rows
                if (row.get("Path") or row.get("path")) == imported["path"]
            ]
            if not matches:
                return None
            endpoint = f"/Videos/{quote(str(matches[0].get('Id') or matches[0].get('id')), safe='')}/stream"
            params = {"static": "true"}
            if self.family == "lidarr":
                endpoint = endpoint.replace("/Videos/", "/Audio/")
        elif service == "plex":
            result = _get(
                self.media,
                f"/library/sections/{quote(record['media_library_id'], safe='')}/all",
                params={"title": record["baseline"]["title"]},
            )
            rows = result.get("MediaContainer", {}).get("Metadata", [])
            parts = [
                part
                for row in rows
                for media in row.get("Media") or []
                for part in media.get("Part") or []
                if part.get("file") == imported["path"]
            ]
            if not parts:
                return None
            endpoint, params = parts[0]["key"], {"download": 1}
        else:
            result = _get(
                self.media,
                f"/api/libraries/{quote(record['media_library_id'], safe='')}/items",
                params={"limit": 100},
            )
            rows = result.get("results", [])
            matches = [
                row
                for row in rows
                if row.get("path") == os.path.dirname(imported["path"])
            ]
            if not matches:
                return None
            details = _get(self.media, f"/api/items/{quote(matches[0]['id'], safe='')}")
            audio = (details.get("media") or {}).get("audioFiles") or []
            selected = next(
                (
                    row
                    for row in audio
                    if (row.get("metadata") or {}).get("path") == imported["path"]
                ),
                None,
            )
            if selected is None:
                return None
            endpoint, params = (
                f"/api/items/{quote(matches[0]['id'], safe='')}/file/{quote(selected['ino'], safe='')}",
                {},
            )
        if (
            not isinstance(endpoint, str)
            or not endpoint.startswith("/")
            or endpoint.startswith("//")
        ):
            raise ProbeError("The media server returned an unsupported streaming path.")
        return endpoint, params

    def playback(self, record):
        selected = self._playback_endpoint(record)
        if selected is None:
            return None
        endpoint, params = selected
        imported = record["imported"]
        content = bytearray()
        for offset in range(0, int(imported["size"]), 1048576):
            end = min(offset + 1048576, int(imported["size"])) - 1
            response = self.media.request(
                "GET",
                endpoint,
                params=params,
                headers={"Range": f"bytes={offset}-{end}"},
            )
            response.raise_for_status()
            expected = end - offset + 1
            if len(response.content) != expected or (
                offset and response.status_code != 206
            ):
                raise ProbeError(
                    "The media server did not honor the bounded playback range."
                )
            content.extend(response.content)
        return bytes(content)

    def cleanup(self, record):
        self.connect(write=True)
        if record.get("baseline", {}).get("binding") != self.binding():
            raise ProbeError(
                "The server endpoints changed; restore the original bindings before owned cleanup."
            )
        if record.get("import_intent") and record.get("command_id"):
            status = _get(self.arr, f"{self.prefix}/command/{record['command_id']}")
            if str(status.get("status", "")).lower() not in {
                "completed",
                "failed",
                "aborted",
                "cancelled",
            }:
                raise ProbeError(
                    "Wait for the owned Arr import command to stop before cleanup."
                )
        imported = record.get("imported")
        # Native completed-download handling can import and remove the provider
        # job before this observer records an import intent. Native history must
        # still prove ownership before any file is removed.
        if record.get("job_id") and not imported and self.files(self.item()):
            imported = self.imported(record)
            if imported is None:
                raise ProbeError(
                    "The new file's ownership is not established by native history. It was preserved for review."
                )
            record["imported"] = imported
        if record.get("job_id") and not imported:
            queue = _get(
                self.arr, self.prefix + "/queue", params={"page": 1, "pageSize": 1000}
            )
            if (
                not isinstance(queue, dict)
                or type(queue.get("totalRecords")) is not int
                or not isinstance(queue.get("records"), list)
                or not 0 <= queue["totalRecords"] <= 1000
                or len(queue["records"]) != queue["totalRecords"]
                or any(not isinstance(row, dict) for row in queue["records"])
            ):
                raise ProbeError(
                    "The native Arr queue cannot be completely checked before cleanup. Owned artifacts were preserved."
                )
            if any(
                row.get("downloadId") == record["job_id"] for row in queue["records"]
            ):
                raise ProbeError(
                    "The native Arr still tracks this test job and may be importing it. Wait for native completion before owned cleanup; its source and media were preserved."
                )
        if self.options.get("cleanup_after", True) and imported:
            kind = (
                "episodefile"
                if self.family == "sonarr"
                else "trackfile" if self.family == "lidarr" else "moviefile"
            )
            response = self.arr.request("GET", f"{self.prefix}/{kind}/{imported['id']}")
            if response.status_code != 404:
                response.raise_for_status()
                row = response.json()
                if (
                    _digest(
                        {
                            key: row.get(key)
                            for key in ("id", "path", "size", "dateAdded")
                        }
                    )
                    != imported["fingerprint"]
                ):
                    raise ProbeError(
                        "The imported test file changed outside this run; automatic deletion was refused."
                    )
                self.arr.request(
                    "DELETE", f"{self.prefix}/{kind}/{imported['id']}"
                ).raise_for_status()
                if (
                    self.arr.request(
                        "GET", f"{self.prefix}/{kind}/{imported['id']}"
                    ).status_code
                    != 404
                ):
                    raise ProbeError(
                        "The Arr has not confirmed removal of the owned test file."
                    )
            self.refresh_media(record, removed=True)
            deadline = time.monotonic() + 10
            while self._playback_endpoint(record) is not None:
                if time.monotonic() >= deadline:
                    raise ProbeError(
                        "Waiting for the media server to retire the removed test file from its index."
                    )
                time.sleep(0.2)
        if record.get("job_id"):
            for mode, row in self._jobs(record["marker"]):
                if row.get("nzo_id") == record["job_id"]:
                    self.sab(
                        mode,
                        name="delete",
                        value=self.job_id(record["job_id"]),
                        archive=0,
                        del_files=1 if self.options.get("cleanup_after", True) else 0,
                    )
            if any(
                row.get("nzo_id") == record["job_id"]
                for _, row in self._jobs(record["marker"])
            ):
                raise ProbeError(
                    "The provider has not confirmed removal of the owned test job."
                )


def _options(action, payload):
    values = (payload.get("arrangement") or {}).get("values") or {}
    options = copy.deepcopy(values.get(f"{action['source']}::workflow_probe") or {})
    if not isinstance(options, dict):
        raise ProbeError("Use the advertised workflow test fields.")
    if not options:
        return None
    for name, default, minimum, maximum in (
        ("max_download_bytes", 1048576, 1024, MAX_CONTENT),
        ("timeout_seconds", 120, 30, 300),
        ("item_id", 0, 1, 2147483647),
    ):
        value = options.setdefault(name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ProbeError("Choose a valid item ID and bounded size/time budgets.")
    if options.get("provider") not in {"infinidysk", "decypharr", "altmount"}:
        raise ProbeError("Choose a managed provider for the test.")
    if options.get("media_server") not in {
        "plex",
        "jellyfin",
        "emby",
        "audiobookshelf",
    }:
        raise ProbeError("Choose a managed playback server for the test.")
    options.setdefault("cleanup_after", True)
    options.setdefault("download_client_id", 0)
    if (
        type(options["download_client_id"]) is not int
        or not 0 <= options["download_client_id"] <= 2147483647
    ):
        raise ProbeError(
            "Choose a valid native download client ID, or leave it at zero for an unambiguous automatic choice."
        )
    return options


def _nzb(options, factory):
    source = options.get("source_url")
    if not isinstance(source, str):
        raise ProbeError("Provide the URL of a small authorized NZB test file.")
    parts = urlsplit(source)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise ProbeError("Use an HTTP(S) NZB URL without embedded login credentials.")
    client = factory(
        urlunsplit((parts.scheme, parts.netloc, "", "", "")),
        allow_private=options.get("allow_private_source") is True,
    )
    response = client.request(
        "GET", urlunsplit(("", "", parts.path or "/", parts.query, ""))
    )
    response.raise_for_status()
    if len(response.content) > 262144:
        raise ProbeError("Use a small NZB manifest for this bounded test.")
    document = ET.fromstring(response.content)
    files = [row for row in document.iter() if row.tag.rsplit("}", 1)[-1] == "file"]
    segments = [
        row for row in document.iter() if row.tag.rsplit("}", 1)[-1] == "segment"
    ]
    if len(files) != 1 or not segments or len(segments) > 256:
        raise ProbeError("Use a single-file NZB with at most 256 segments.")
    try:
        sizes = [int(row.attrib["bytes"]) for row in segments]
    except (KeyError, ValueError):
        raise ProbeError("Every NZB segment must declare its size.") from None
    if any(size <= 0 for size in sizes) or sum(sizes) > options["max_download_bytes"]:
        raise ProbeError("The NZB exceeds the selected content budget.")
    return response.content, sum(sizes)


def _decoder(content, *, audio=False, cancelled=lambda: False):
    if not content or len(content) > MAX_CONTENT:
        raise ProbeError("The playback sample is empty or exceeds its budget.")
    child = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "utils.arrangement_decode_child",
            "audio" if audio else "video",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline, pending = time.monotonic() + 8, content
    try:
        while True:
            if cancelled() or time.monotonic() >= deadline:
                raise ProbeError(
                    "The bounded playback decoder was cancelled or timed out."
                )
            try:
                output, _ = child.communicate(pending, timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pending = None
        frames = [
            line
            for line in output.decode("ascii", errors="ignore").splitlines()
            if re.match(r"^\s*\d+,\s*-?\d+,", line)
        ]
        if child.returncode != 0 or len(frames) != 1:
            raise ProbeError("The media server's sample did not decode into a frame.")
        return 1
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=2)
        for stream in (child.stdin, child.stdout):
            if stream is not None:
                stream.close()


class WorkflowProbe:
    def __init__(
        self,
        action,
        config,
        payload,
        directory,
        *,
        client_factory=ApiClient,
        cancelled=lambda: False,
        clock=time.monotonic,
        sleeper=time.sleep,
    ):
        from utils.arrangement_state import SetupStore

        self.action, self.config, self.payload = action, config, payload
        self.factory, self.cancelled, self.clock, self.sleeper = (
            client_factory,
            cancelled,
            clock,
            sleeper,
        )
        self.options = _options(action, payload)
        scope = (
            str(action.get("_setup_run_id") or "standalone") + "::" + action["source"]
        )
        self.identifier = hashlib.sha256(scope.encode()).hexdigest()[:32]
        self.store = SetupStore(directory)
        try:
            self.prune()
            self.record = self.store.read("run", self.identifier)
        except FileNotFoundError:
            self.record = {}
        except Exception:
            self.store.close()
            raise
        self.deadline = self.clock() + (self.options or {}).get("timeout_seconds", 120)
        self.native = NativeWorkflow(
            action, config, payload, self.options or {}, client_factory
        )

    def prune(self):
        records = []
        for identifier in self.store.records("run"):
            value = self.store.read("run", identifier)
            if value.get("cleanup_complete") is True:
                records.append((value.get("verified_at") or "", identifier))
        for _, identifier in sorted(records)[:-50]:
            if identifier != self.identifier:
                self.store.remove("run", identifier)
        if len(
            self.store.records("run")
        ) >= 100 and self.identifier not in self.store.records("run"):
            self.store.close()
            raise ProbeError(
                "Complete the retained workflow cleanup tasks before creating another test journal."
            )

    def save(self):
        self.store.write("run", self.identifier, self.record)

    def check(self, label, ok=True, reason=""):
        self.record.setdefault("checks", []).append(
            {"label": label, "ok": ok, "reason": reason}
        )
        self.save()

    def wait(self, callback):
        while not self.cancelled() and self.clock() < self.deadline:
            value = callback()
            if value:
                return value
            self.sleeper(min(1, max(0.01, self.deadline - self.clock())))
        raise ProbeError("The workflow test was cancelled or reached its time budget.")

    def active(self):
        if self.cancelled() or self.clock() >= self.deadline:
            raise ProbeError(
                "The workflow test was cancelled or reached its time budget."
            )

    def preview(self):
        if not self.options:
            return _result("declined", "No complete workflow test was selected.")
        if (
            self.record.get("stage") != "preview"
            and self.record
            and not self.record.get("cleanup_complete")
        ):
            raise ProbeError(
                "Finish or clean up the previous owned test before preparing another."
            )
        manifest, count = _nzb(self.options, self.factory)
        marker_suffix = "DUMB.Setup.Probe." + uuid.uuid4().hex
        baseline = self.native.prepare(write=False, marker=marker_suffix)
        self.record = {
            "stage": "preview",
            "preview_id": uuid.uuid4().hex,
            "options_signature": _digest(_scope_options(self.options)),
            "signature": _digest(
                {
                    "options": _scope_options(self.options),
                    "manifest": hashlib.sha256(manifest).hexdigest(),
                    "baseline": baseline,
                }
            ),
            "declared_bytes": count,
            "checks": [],
            "marker_suffix": marker_suffix,
            "marker": baseline.get("job_name", marker_suffix),
            "cleanup_complete": True,
        }
        self.save()
        refresh = (
            "Scans the containing Audiobookshelf library; this requires separate explicit consent."
            if self.options["media_server"] == "audiobookshelf"
            else (
                "Scans the containing media library because you explicitly selected that additional scope."
                if self.options.get("authorize_library_scan") is True
                and self.options["media_server"] in {"jellyfin", "emby"}
                else "Refreshes the imported media path."
            )
        )
        return _result(
            "planned",
            "Review this real test's effects and budgets, then explicitly authorize execution.",
            evidence={
                "level": "probe_preview",
                "preview_id": self.record["preview_id"],
                "preview_status": "ready",
                "requires_consent": True,
                "changes": [
                    {
                        "field": "Workflow test",
                        "before": "No content test",
                        "after": f"Enqueue {count} declared bytes with oversize cancellation; import only the selected fileless item and decode one streamed frame.",
                        "managed": True,
                        "requires_consent": True,
                    }
                ],
                "side_effects": [
                    "Uses the selected provider account and connection allocation. Declared size is not a hard network transfer limit.",
                    refresh,
                    "The test uses a reserved unmonitored item and observes native automatic import under your existing policy; do not change that item until the test finishes.",
                    (
                        f"The selected scanner covers {(baseline.get('scan_scope') or {}).get('library_count', 0)} configured libraries; multiple-library scanning needs separate consent."
                        if baseline.get("scan_scope")
                        else "No additional server-wide library scan was selected."
                    ),
                    "Removes only test-owned artifacts when cleanup is selected.",
                ],
            },
        )

    def run(self):
        if not self.options:
            return _result("declined", "No complete workflow test was selected.")
        values = (self.payload.get("arrangement") or {}).get("values") or {}
        if values.get(f"{self.action['source']}::workflow_cleanup") is True:
            return self.cleanup_only()
        if self.record.get("options_signature") != _digest(
            _scope_options(self.options)
        ):
            raise ProbeError(
                "The workflow choices changed. Review a fresh preview before running this test."
            )
        if self.options.get("authorize_download_import_scan") is not True or values.get(
            f"{self.action['source']}::workflow_preview_id"
        ) != self.record.get("preview_id"):
            raise ProbeError(
                "Preview this exact workflow and explicitly authorize its effects before running it."
            )
        if (
            self.options["media_server"] == "audiobookshelf"
            and self.options.get("authorize_library_scan") is not True
        ):
            raise ProbeError(
                "Explicitly authorize Audiobookshelf's containing-library scan before this test can enqueue anything."
            )
        if self.options.get("authorize_reserved_test_item") is not True:
            raise ProbeError(
                "Explicitly reserve the unmonitored fileless item and leave it unchanged until this test finishes."
            )
        if self.record.get("stage") in {"complete", "failed", "cleaned"}:
            return self.report()
        self.active()
        self.native.connect(write=True)
        if self.record.get("stage") == "preview":
            manifest, count = _nzb(self.options, self.factory)
            baseline = self.native.prepare(
                write=True, marker=self.record["marker_suffix"]
            )
            if self.record.get("signature") != _digest(
                {
                    "options": _scope_options(self.options),
                    "manifest": hashlib.sha256(manifest).hexdigest(),
                    "baseline": baseline,
                }
            ):
                raise ProbeError(
                    "The test source, target state or choices changed. Review a fresh preview."
                )
            self.active()
            self.record.update(
                stage="enqueue",
                cleanup_complete=False,
                baseline=baseline,
            )
            self.save()
            self.record["job_id"] = self.native.enqueue(manifest, self.record["marker"])
            self.record["stage"] = "download"
            self.save()
        if self.record.get("stage") == "enqueue":
            self.record["job_id"] = self.native.recover_job(self.record["marker"])
            self.record["stage"] = "download"
            self.save()
        try:
            if self.record.get("stage") == "download":

                def progress():
                    downloaded = self.native.downloaded(self.record)
                    if downloaded:
                        return {"download": downloaded}
                    # Arr may delete completed provider history immediately.
                    # Its exact download-ID import event is independent proof
                    # of completion; absence alone never authorizes a retry.
                    imported = self.native.imported(self.record)
                    return {"imported": imported} if imported else None

                completed = self.wait(progress)
                if completed.get("imported"):
                    self.record.update(
                        imported=completed["imported"],
                        import_intent={
                            "automatic": True,
                            "download_id": self.record["job_id"],
                        },
                        stage="playback",
                    )
                    self.check(
                        "Native Arr history proves this job completed and its file was automatically imported"
                    )
                    self.check(
                        "Selected Arr imported the reserved test item under its existing policy"
                    )
                else:
                    self.record.update(download=completed["download"], stage="import")
                    self.check(
                        "Provider completed its native job; content is checked during playback"
                    )
            if self.record.get("stage") == "import":
                self.active()
                self.native.begin_import(self.record, self.save)
                imported = self.wait(lambda: self.native.imported(self.record))
                self.record.update(imported=imported, stage="playback")
                self.check(
                    "Selected Arr imported a new file without replacing existing media"
                )
            if self.record.get("stage") == "playback":
                self.active()
                self.native.refresh_media(self.record)
                stream = self.wait(lambda: self.native.playback(self.record))
                self.record["decoded_frames"] = _decoder(
                    stream,
                    audio=self.action["service_key"] == "lidarr",
                    cancelled=self.cancelled,
                )
                self.record.update(
                    stage="cleanup", verified_at=datetime.now(timezone.utc).isoformat()
                )
                self.check("The media server streamed a sample and one frame decoded")
            if self.record.get("stage") == "cleanup":
                self.native.cleanup(self.record)
                self.record.update(stage="complete", cleanup_complete=True)
                self.check(
                    "Test-owned artifacts cleaned up"
                    if self.options.get("cleanup_after", True)
                    else "Test artifacts retained by explicit choice"
                )
            return self.report()
        except Exception:
            self.record["failed_stage"] = self.record.get("stage")
            try:
                self.native.cleanup(self.record)
                self.record["cleanup_complete"] = True
            except Exception:
                self.record["cleanup_complete"] = False
            self.record["stage"] = "failed"
            self.check(
                "Workflow test did not complete",
                False,
                "Inspect the named stage and retry owned cleanup; no unrelated files or jobs are removed.",
            )
            raise

    def cleanup_only(self):
        if not self.record or self.record.get("stage") == "preview":
            return _result("declined", "There is no enqueued test to clean up.")
        if self.record.get("options_signature") != _digest(
            _scope_options(self.options)
        ):
            raise ProbeError(
                "Restore the original test choices and endpoints before cleaning up its owned artifacts."
            )
        self.native.connect(write=True)
        if self.record.get("stage") == "enqueue" and not self.record.get("job_id"):
            self.record["job_id"] = self.native.recover_job(self.record["marker"])
            self.save()
        self.native.cleanup(self.record)
        self.record.update(stage="cleaned", cleanup_complete=True)
        self.check("Owned test cleanup confirmed; no new content operation ran")
        return self.report()

    def report(self, *, check_scope=False):
        passed = (
            self.record.get("stage") == "complete"
            and self.record.get("decoded_frames") == 1
            and self.record.get("cleanup_complete") is True
        )
        if passed and check_scope:
            if self.record.get("options_signature") != _digest(
                _scope_options(self.options or {})
            ):
                passed = False
            else:
                self.native.connect(write=False)
                passed = self.native.binding() == self.record.get("baseline", {}).get(
                    "binding"
                )
        return _result(
            "applied" if passed else "waiting_input",
            (
                "Recorded workflow test result; another download requires a new explicit preview."
                if passed
                else "Preview and run the optional test explicitly, or finish its owned cleanup."
            ),
            "passed" if passed else "not_checked",
            {
                "level": "workflow_test",
                **{
                    key: self.record.get(key)
                    for key in (
                        "stage",
                        "failed_stage",
                        "checks",
                        "decoded_frames",
                        "cleanup_complete",
                        "verified_at",
                        "declared_bytes",
                    )
                },
            },
        )


def workflow_action(
    action,
    config,
    payload,
    operation,
    *,
    read_only=False,
    directory="/config/.dumb-setup/workflows",
    client_factory=ApiClient,
    cancelled=lambda: False,
):
    if action.get("action") != "workflow_test":
        return None
    probe = None
    try:
        probe = WorkflowProbe(
            action,
            config,
            payload,
            directory,
            client_factory=client_factory,
            cancelled=cancelled,
        )
        if operation == "preview":
            return probe.preview()
        if read_only or operation == "recheck":
            return probe.report(check_scope=True)
        if operation != "apply":
            return _result(
                "waiting_input",
                "A complete workflow test always requires explicit Apply after Preview.",
            )
        return probe.run()
    except (ApiError, ValueError, OSError, ET.ParseError) as error:
        evidence = probe.report().get("evidence") if probe is not None else {}
        return _result(
            "waiting_input",
            (
                str(error)
                if isinstance(error, ProbeError)
                else "The workflow test could not complete. Review its source, authorization, size budget and native target state; inspect the recorded stage before retrying."
            ),
            "failed",
            evidence,
        )
    finally:
        if probe is not None:
            probe.store.close()
