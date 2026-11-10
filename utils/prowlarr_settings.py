from contextvars import ContextVar
from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
from utils.core_services import get_core_services
from utils.url_security import safe_request, safe_urlopen
from utils.user_management import chown_recursive
from typing import Optional, Tuple
import defusedxml.ElementTree as ET
import json, os, time, urllib.request, urllib.error
import tempfile
import zipfile
import threading
import copy
import hashlib
from utils.integration_result import ActionResult, emit_action

_CONFIG_OVERRIDE = ContextVar("arr_settings_configuration", default=None)


def _setting(key, default=None):
    configuration = _CONFIG_OVERRIDE.get()
    return (
        configuration.get(key, default)
        if configuration is not None
        else CONFIG_MANAGER.get(key, default)
    )


ARR_SERVICES = [
    "sonarr",
    "radarr",
    "lidarr",
    "readarr",
    "whisparr",
    "whisparr-v3",
]

ARR_APP_MAP = {
    "sonarr": "Sonarr",
    "radarr": "Radarr",
    "lidarr": "Lidarr",
    "readarr": "Readarr",
    "whisparr": "Whisparr",
    "whisparr-v3": "Whisparr",
}

WHISPARR_SYNC_CATEGORIES = [
    6000,  # XXX
    6010,
    6020,
    6030,
    6040,
    6045,
    6050,
    6060,
    6070,
    6080,
    6090,
    # Zilean's native Torznab endpoint advertises generic Movies/TV caps.
    # Include them so Prowlarr can sync it to Whisparr without failing category validation.
    2000,
    2010,
    2020,
    2030,
    2040,
    2045,
    2050,
    2060,
    2070,
    2080,
    5000,
    5010,
    5020,
    5030,
    5040,
    5045,
    5050,
    5060,
    5070,
    5080,
]

WHISPARR_CUSTOM_CATEGORY = "XXX"

CUSTOM_INDEXER_URLS = {
    "stremthru.yml": "https://raw.githubusercontent.com/dreulavelle/Prowlarr-Indexers/main/Custom/stremthru.yml",
    "zilean.yml": "https://raw.githubusercontent.com/dreulavelle/Prowlarr-Indexers/main/Custom/zilean.yml",
}
CUSTOM_INDEXER_REPO_ZIP = (
    "https://github.com/dreulavelle/Prowlarr-Indexers/archive/refs/heads/main.zip"
)

# The upstream bundle currently includes both filenames with the same Cardigann id/name.
# Keeping both makes Prowlarr reject one definition and can orphan managed indexers.
CUSTOM_INDEXER_DUPLICATE_FILES = {
    "elfhosted-internal.yml": "elfhosted-torrentio.yml",
}

_INDEXER_SCHEMA_LOGGED = False
_CUSTOM_INDEXER_SYNC_LOCK = threading.Lock()
_PROWLARR_PATCH_LOCK = threading.RLock()
_CUSTOM_INDEXER_SYNC_TS = 0.0


def _read_custom_indexer_identity(path: str) -> tuple[str, str]:
    indexer_id = ""
    indexer_name = ""
    try:
        with open(path, "r") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith("id:") and not indexer_id:
                    indexer_id = stripped.split(":", 1)[1].strip().strip("\"'")
                elif stripped.startswith("name:") and not indexer_name:
                    indexer_name = stripped.split(":", 1)[1].strip().strip("\"'")
                if indexer_id and indexer_name:
                    break
    except OSError:
        return "", ""
    return indexer_id, indexer_name


def _prune_duplicate_custom_indexers(custom_dir: str) -> None:
    for duplicate, canonical in CUSTOM_INDEXER_DUPLICATE_FILES.items():
        duplicate_path = os.path.join(custom_dir, duplicate)
        canonical_path = os.path.join(custom_dir, canonical)
        if not os.path.exists(duplicate_path) or not os.path.exists(canonical_path):
            continue
        duplicate_identity = _read_custom_indexer_identity(duplicate_path)
        canonical_identity = _read_custom_indexer_identity(canonical_path)
        if duplicate_identity != canonical_identity or not any(duplicate_identity):
            logger.warning(
                "Keeping Prowlarr custom indexer %s because it no longer duplicates %s.",
                duplicate,
                canonical,
            )
            continue
        try:
            os.remove(duplicate_path)
            logger.info(
                "Removed duplicate Prowlarr custom indexer definition %s; using %s.",
                duplicate,
                canonical,
            )
        except OSError as exc:
            logger.warning(
                "Failed to remove duplicate Prowlarr custom indexer %s: %s",
                duplicate,
                exc,
            )


def _replace_links_block(content: str, links: list[str]) -> str:
    lines = content.splitlines()
    out = []
    i = 0
    replaced = False
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("links:"):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(line)
            i += 1
            while i < len(lines):
                next_line = lines[i]
                if next_line.strip().startswith("-") and next_line.startswith(
                    (indent + "  ", indent + "-")
                ):
                    i += 1
                    continue
                break
            list_indent = indent + "  "
            for link in links:
                out.append(f"{list_indent}- {link}")
            replaced = True
            continue
        out.append(line)
        i += 1
    if not replaced:
        out.append("links:")
        for link in links:
            out.append(f"  - {link}")
    trailing_newline = "\n" if content.endswith("\n") else ""
    return "\n".join(out) + trailing_newline


def _ensure_custom_indexer_whisparr_caps(filename: str, content: str) -> str:
    if filename not in {"stremthru.yml", "zilean.yml"}:
        return content

    updated = content
    categories_inline = "caps: categories: Movies: Movies TV: TV"
    if (
        categories_inline in updated
        and f"{WHISPARR_CUSTOM_CATEGORY}: {WHISPARR_CUSTOM_CATEGORY}" not in updated
    ):
        updated = updated.replace(
            categories_inline,
            f"{categories_inline} {WHISPARR_CUSTOM_CATEGORY}: {WHISPARR_CUSTOM_CATEGORY}",
            1,
        )
    elif f"{WHISPARR_CUSTOM_CATEGORY}: {WHISPARR_CUSTOM_CATEGORY}" not in updated:
        lines = updated.splitlines()
        in_caps = False
        in_categories = False
        categories_indent = ""
        for index, line in enumerate(lines):
            stripped = line.strip()
            indent = line[: len(line) - len(line.lstrip())]
            if stripped == "caps:":
                in_caps = True
                in_categories = False
                continue
            if in_caps and stripped == "categories:":
                in_categories = True
                categories_indent = indent
                continue
            if in_categories and stripped == "TV: TV":
                lines.insert(
                    index + 1,
                    f"{indent}{WHISPARR_CUSTOM_CATEGORY}: {WHISPARR_CUSTOM_CATEGORY}",
                )
                trailing_newline = "\n" if updated.endswith("\n") else ""
                updated = "\n".join(lines) + trailing_newline
                break
            if in_categories and indent <= categories_indent and stripped:
                in_categories = False
            if in_caps and not in_categories and not line.startswith(" ") and stripped:
                in_caps = False

    if filename == "zilean.yml":
        lines = [
            line
            for line in updated.splitlines()
            if not line.strip().startswith("Category:")
        ]
        trailing_newline = "\n" if updated.endswith("\n") else ""
        updated = "\n".join(lines) + trailing_newline
        if 'args: ["xxx", "XXX"]' not in updated:
            needle = '- name: replace args: ["movie", "Movies"]'
            updated = updated.replace(
                needle,
                f'{needle} - name: replace args: ["xxx", "XXX"]',
                1,
            )

    if filename == "stremthru.yml":
        updated = updated.replace(
            ':{{ if .Query.Ep }}{{ .Query.Ep }}{{ else }}1{{ end }}"',
            ":{{ if .Query.Ep }}{{ .Query.Ep }}{{ else }}1{{ end }}",
        )
        if "  categorymappings:" not in updated:
            category_map = "\n".join(
                [
                    "  categorymappings:",
                    "    - {id: 2000, cat: Movies, desc: Movies}",
                    "    - {id: 5000, cat: TV, desc: TV}",
                    "    - {id: 6000, cat: XXX, desc: XXX}",
                ]
            )
            updated = updated.replace(
                "  categories:\n    Movies: Movies\n    TV: TV\n    XXX: XXX",
                category_map,
            )
            updated = updated.replace(
                "  categories:\n    Movies: Movies\n    TV: TV",
                category_map,
            )
        updated = updated.replace(
            f"categories: [Movies, {WHISPARR_CUSTOM_CATEGORY}]",
            "categories: [2000]",
        )
        updated = updated.replace("categories: [Movies]", "categories: [2000]")
        updated = updated.replace(
            f"categories: [TV, {WHISPARR_CUSTOM_CATEGORY}]",
            "categories: [5000]",
        )
        updated = updated.replace("categories: [TV]", "categories: [5000]")
        updated = updated.replace(
            "text: '{{ if .Categories }}{{ join .Categories \",\" }}{{ else }}{{ if .Result.category_is_tv_show }}TV{{ else }}Movies{{ end }}{{ end }}'",
            'text: "{{ if .Result.category_is_tv_show }}5000{{ else }}2000{{ end }}"',
        )
        updated = updated.replace(
            'text: "{{ if .Result.category_is_tv_show }}TV{{ else }}Movies{{ end }}"',
            'text: "{{ if .Result.category_is_tv_show }}5000{{ else }}2000{{ end }}"',
        )

    return updated


def ensure_custom_indexers(config_dir: str, zilean_port: int) -> None:
    global _CUSTOM_INDEXER_SYNC_TS
    with _CUSTOM_INDEXER_SYNC_LOCK:
        now = time.time()
        if now - _CUSTOM_INDEXER_SYNC_TS < 60:
            return
        _CUSTOM_INDEXER_SYNC_TS = now
    root_dir = config_dir
    indexer_root = os.path.join(config_dir, "indexer")
    if os.path.isdir(indexer_root):
        root_dir = indexer_root
    definitions_dir = os.path.join(root_dir, "Definitions")
    custom_dir = os.path.join(definitions_dir, "Custom")
    os.makedirs(custom_dir, exist_ok=True)
    links_map = {
        "stremthru.yml": [
            "https://stremthru.13377001.xyz/v0/torznab",
            "http://stremthru:8080",
        ],
        "zilean.yml": [
            f"http://127.0.0.1:{zilean_port}",
            "https://zileanfortheweebs.midnightignite.me",
        ],
    }

    def _write_with_links(path: str, content: str, link_overrides: list[str]) -> None:
        updated = _replace_links_block(content, link_overrides)
        updated = _ensure_custom_indexer_whisparr_caps(os.path.basename(path), updated)
        with open(path, "w") as handle:
            handle.write(updated)

    try:
        with safe_urlopen(CUSTOM_INDEXER_REPO_ZIP, timeout=60) as resp:
            zip_bytes = resp.read()
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "custom.zip")
            with open(zip_path, "wb") as handle:
                handle.write(zip_bytes)
            with zipfile.ZipFile(zip_path) as archive:
                for entry in archive.namelist():
                    if not entry.endswith(".yml"):
                        continue
                    parts = entry.split("/")
                    if len(parts) < 3 or parts[-2] != "Custom":
                        continue
                    filename = parts[-1]
                    target_path = os.path.join(custom_dir, filename)
                    if os.path.exists(target_path) and filename not in links_map:
                        continue
                    with archive.open(entry) as src:
                        content = src.read().decode("utf-8")
                    if filename in links_map:
                        _write_with_links(target_path, content, links_map[filename])
                    elif not os.path.exists(target_path):
                        with open(target_path, "w") as handle:
                            handle.write(content)
    except Exception as exc:
        logger.warning("Failed to sync Prowlarr custom indexers: %s", exc)

    for filename, url in CUSTOM_INDEXER_URLS.items():
        target_path = os.path.join(custom_dir, filename)
        if os.path.exists(target_path):
            try:
                with open(target_path, "r") as handle:
                    raw = handle.read()
                _write_with_links(target_path, raw, links_map.get(filename, []))
                continue
            except Exception as exc:
                logger.warning(
                    "Failed to update Prowlarr custom indexer %s: %s",
                    filename,
                    exc,
                )
        try:
            with safe_urlopen(url, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
            _write_with_links(target_path, raw, links_map.get(filename, []))
        except Exception as exc:
            logger.warning(
                "Failed to sync Prowlarr custom indexer %s: %s", filename, exc
            )
    _prune_duplicate_custom_indexers(custom_dir)

    try:
        user_id = int(_setting("puid"))
        group_id = int(_setting("pgid"))
        chown_recursive(definitions_dir, user_id, group_id)
    except Exception as exc:
        logger.warning("Failed to update ownership for %s: %s", definitions_dir, exc)


def _parse_arr_api_key(config_xml_path: str) -> str:
    try:
        if not (config_xml_path and os.path.exists(config_xml_path)):
            return ""
        tree = ET.parse(config_xml_path)
        root = tree.getroot()
        node = root.find(".//ApiKey")
        if node is not None and (node.text or "").strip():
            return node.text.strip()
    except Exception as e:
        logger.warning("Failed reading ApiKey from %s: %s", config_xml_path, e)
    return ""


def _wait_for_api_key(
    config_xml_path: str, timeout_s: int = 60, interval_s: float = 2.0
) -> str:
    deadline = time.time() + max(1, timeout_s)
    while time.time() < deadline:
        token = _parse_arr_api_key(config_xml_path)
        if token:
            return token
        time.sleep(interval_s)
    return ""


def _collect_arr_entries() -> list[dict]:
    entries = []
    for svc_name in ARR_SERVICES:
        svc_cfg = _setting(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst_key, inst in instances.items():
            if not isinstance(inst, dict) or not inst.get("enabled"):
                continue
            port = inst.get("port") or inst.get("host_port")
            try:
                port = str(int(port)) if port is not None else None
            except Exception:
                port = None
            if not port:
                logger.warning(
                    "Skipping %s instance %s: missing port", svc_name, inst_key
                )
                continue
            host = _native_host(inst, port)
            cfg_path = (inst.get("config_file") or "").strip()
            token = _parse_arr_api_key(cfg_path)
            if not token:
                logger.warning(
                    "Skipping %s instance %s: missing API key", svc_name, inst_key
                )
            core_services = get_core_services(inst)
            entries.append(
                {
                    "service": svc_name,
                    "instance": inst_key,
                    "host": host,
                    "api_key": token,
                    "core_services": core_services,
                }
            )
    return entries


def _native_host(instance: dict, port) -> str:
    base = ""
    path = instance.get("config_file") or ""
    if path and os.path.isfile(path):
        try:
            base = (
                (ET.parse(path).getroot().findtext(".//UrlBase") or "")
                .strip()
                .strip("/")
            )
        except (ET.ParseError, OSError):
            pass
    return f"http://127.0.0.1:{port}" + (f"/{base}" if base else "")


def _api_request(host, token, resource, method="GET", data=None):
    """Negotiate supported Prowlarr API routes without retrying rejected writes."""
    for version in ("v1", "v3"):
        try:
            return _prowlarr_req(
                _join(host, f"/api/{version}/{resource}"), token, method, data
            )
        except urllib.error.HTTPError as exc:
            if exc.code not in {404, 405} or version == "v3":
                raise
    raise RuntimeError("Prowlarr API resource unavailable")


def _ownership_endpoint_scope(instance):
    port = int(instance.get("port") or instance.get("host_port") or 0)
    host = _native_host(instance, port)
    key = _parse_arr_api_key(instance.get("config_file") or "")
    return hashlib.sha256(
        json.dumps(
            {"family": "prowlarr", "url": host, "key": key}, sort_keys=True
        ).encode()
    ).hexdigest()


def _load_ownership(instance):
    config_dir = instance.get("config_dir")
    if not config_dir:
        raise ValueError("Prowlarr config directory is required for safe ownership")
    path = os.path.join(config_dir, ".dumb-setup-ownership.json")
    if os.path.islink(path):
        raise ValueError("Prowlarr ownership metadata must be a regular file")
    if not os.path.exists(path):
        return {"applications": {}, "indexers": {}}
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if (
        not isinstance(value, dict)
        or any(
            not isinstance(value.get(key), dict) for key in ("applications", "indexers")
        )
        or any(
            not isinstance(identifier, int)
            for key in ("applications", "indexers")
            for identifier in value[key].values()
        )
    ):
        raise ValueError("Prowlarr ownership metadata is invalid")
    if "signatures" in value and (
        not isinstance(value["signatures"], dict)
        or any(
            not isinstance(signature, str) for signature in value["signatures"].values()
        )
    ):
        raise ValueError("Prowlarr ownership signatures are invalid")
    if value.get("_endpoint_scope") != _ownership_endpoint_scope(instance):
        # A new server may reuse native integer IDs. Never adopt those rows on
        # the strength of a previous endpoint's sidecar alone.
        return {"applications": {}, "indexers": {}}
    return value


def _save_ownership(instance, ownership):
    ownership["_endpoint_scope"] = _ownership_endpoint_scope(instance)
    root = instance["config_dir"]
    if os.path.islink(os.path.join(root, ".dumb-setup-ownership.json")):
        raise ValueError("Prowlarr ownership metadata must be a regular file")
    os.makedirs(root, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".dumb-ownership-", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(ownership, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        uid, gid = _setting("puid"), _setting("pgid")
        if uid is not None and gid is not None:
            os.chown(temporary, int(uid), int(gid))
        os.replace(temporary, os.path.join(root, ".dumb-setup-ownership.json"))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _connection_fingerprint(value):
    """Bind an upstream ID to public connection identity without retaining secrets."""
    fields = [
        (str(field.get("name") or "").lower(), field.get("value"))
        for field in value.get("fields") or []
        if str(field.get("name") or "").lower()
        in {"baseurl", "url", "host", "prowlarrurl"}
    ]
    public = {
        "name": value.get("name"),
        "implementation": value.get("implementation"),
        "configContract": value.get("configContract"),
        "endpoints": sorted(fields),
    }
    return hashlib.sha256(
        json.dumps(public, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _owned_row_is_current(ownership, key, row):
    expected = ownership.get("signatures", {}).get(key)
    return not expected or expected == _connection_fingerprint(row)


def _merge_application_payload(existing: dict, desired: dict) -> dict:
    """Own endpoint/key/routing tags; preserve user enablement and sync policy."""
    merged = copy.deepcopy(existing)
    merged["name"] = desired["name"]
    fields = {
        str(field.get("name") or "").lower(): field
        for field in merged.get("fields") or []
    }
    for field in desired.get("fields") or []:
        key = str(field.get("name") or "").lower()
        if key not in {"baseurl", "url", "host", "apikey", "prowlarrurl"}:
            continue
        if key in fields:
            fields[key]["value"] = field.get("value")
        else:
            merged.setdefault("fields", []).append(copy.deepcopy(field))
    merged["tags"] = sorted(
        set(existing.get("tags") or []) | set(desired.get("tags") or [])
    )
    return merged


def _join(host: str, path: str) -> str:
    return f"{host.rstrip('/')}/{path.lstrip('/')}"


def _prowlarr_req(
    url: str,
    key: str,
    method: str = "GET",
    data: Optional[dict] = None,
    timeout: int = 10,
):
    headers = {"X-Api-Key": key, "Accept": "application/json"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8")
    req = safe_request(url, data=body, headers=headers, method=method)
    try:
        with safe_urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception:
                return raw.decode("utf-8")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        setattr(e, "body", body)
        if body:
            logger.warning("Prowlarr API request rejected with HTTP %s.", e.code)
        raise


def _arr_req(
    url: str,
    key: str,
    method: str = "GET",
    data: Optional[dict] = None,
    timeout: int = 10,
):
    headers = {"X-Api-Key": key, "Accept": "application/json"}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8")
    req = safe_request(url, data=body, headers=headers, method=method)
    with safe_urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return raw.decode("utf-8")


def _wait_for_arr(
    host: str,
    token: str,
    timeout_s: int = 30,
    interval_s: float = 2.0,
    api_version: str = "v3",
) -> bool:
    deadline = time.time() + max(1, timeout_s)
    url = _join(host, f"/api/{api_version}/system/status")
    while time.time() < deadline:
        try:
            _arr_req(url, token, "GET", timeout=5)
            return True
        except Exception:
            time.sleep(interval_s)
    return False


def _wait_for_prowlarr(
    host: str, token: str, timeout_s: int = 30, interval_s: float = 2.0
) -> bool:
    deadline = time.time() + max(1, timeout_s)
    url = _join(host, "/api/v1/system/status")
    while time.time() < deadline:
        try:
            _prowlarr_req(url, token, "GET", timeout=5)
            return True
        except Exception:
            time.sleep(interval_s)
    return False


def _get_prowlarr_schemas(host: str, token: str) -> list:
    for path in ("/api/v1/applications/schema", "/api/v3/applications/schema"):
        try:
            data = _prowlarr_req(_join(host, path), token, "GET")
            if isinstance(data, list):
                return data
        except Exception:
            continue
    return []


def _get_prowlarr_indexer_schemas(host: str, token: str) -> list:
    for path in ("/api/v1/indexer/schema", "/api/v3/indexer/schema"):
        try:
            data = _prowlarr_req(_join(host, path), token, "GET")
            if isinstance(data, list):
                return data
        except Exception:
            continue
    return []


def _find_schema(schemas: list, app_name: str) -> Optional[dict]:
    target = (app_name or "").lower()
    for item in schemas:
        impl = (item.get("implementation") or "").lower()
        name = (item.get("implementationName") or "").lower()
        if target in impl or target in name:
            return item
    return None


def _find_indexer_schema(schemas: list, indexer_name: str) -> Optional[dict]:
    target = (indexer_name or "").lower()
    for item in schemas:
        impl = (item.get("implementation") or "").lower()
        name = (item.get("implementationName") or "").lower()
        display = (item.get("name") or "").lower()
        definition = (item.get("definitionName") or "").lower()
        desc = (item.get("description") or "").lower()
        if target in (impl, name, display, definition) or target in desc:
            return item
    return None


def _find_generic_torznab_schema(schemas: list) -> Optional[dict]:
    for item in schemas:
        impl = (item.get("implementation") or "").lower()
        name = (item.get("implementationName") or item.get("name") or "").lower()
        contract = (item.get("configContract") or "").lower()
        values = (impl, name, contract)
        if any("torznab" in value for value in values) and "generic" in name:
            return item
    for item in schemas:
        impl = (item.get("implementation") or "").lower()
        name = (item.get("implementationName") or item.get("name") or "").lower()
        contract = (item.get("configContract") or "").lower()
        values = (impl, name, contract)
        if any("torznab" in value for value in values):
            return item
    return None


def _log_indexer_schema_names(schemas: list) -> None:
    global _INDEXER_SCHEMA_LOGGED
    if _INDEXER_SCHEMA_LOGGED:
        return
    _INDEXER_SCHEMA_LOGGED = True
    names = []
    for item in schemas:
        if not isinstance(item, dict):
            continue
        names.append(
            {
                "implementation": item.get("implementation"),
                "implementationName": item.get("implementationName"),
                "name": item.get("name"),
                "definitionName": item.get("definitionName"),
            }
        )
    logger.debug("Prowlarr indexer schemas: %s", names)


def _build_fields_from_schema(schema: dict, overrides: dict) -> list:
    fields = {}
    for f in schema.get("fields") or []:
        n = f.get("name")
        if not n:
            continue
        fields[n] = f.get("value")

    for key, value in (overrides or {}).items():
        for existing in list(fields.keys()):
            if existing.lower() == key.lower():
                fields[existing] = value
                break

    return [{"name": k, "value": v} for k, v in fields.items()]


def _is_application_current(
    existing: dict, desired: dict, *, masked_credentials_current=False
) -> bool:
    if not existing or not desired:
        return False
    for key in ("enable", "syncLevel", "implementation", "configContract"):
        if (existing.get(key) or "") != (desired.get(key) or ""):
            return False
    # Keep user-managed tags intact: only require DUMB-managed tags to be present.
    existing_tags = {
        int(tag) for tag in (existing.get("tags") or []) if isinstance(tag, int)
    }
    required_tags = {
        int(tag) for tag in (desired.get("tags") or []) if isinstance(tag, int)
    }
    if not required_tags.issubset(existing_tags):
        return False
    existing_fields = {
        (f.get("name") or "").lower(): f.get("value")
        for f in (existing.get("fields") or [])
    }
    desired_fields = {
        (f.get("name") or "").lower(): f.get("value")
        for f in (desired.get("fields") or [])
    }
    for key, value in desired_fields.items():
        if existing_fields.get(key) != value:
            if not (
                key == "apikey"
                and existing_fields.get(key) == "********"
                and masked_credentials_current
            ):
                return False
    return True


def _build_application_payload(
    schema: dict,
    app_name: str,
    arr_host: str,
    arr_api_key: str,
    instance_name: str,
    prowlarr_host: str,
    tag_ids: Optional[list[int]] = None,
) -> dict:
    name = f"{app_name} ({instance_name})"
    overrides = {
        "baseUrl": arr_host,
        "url": arr_host,
        "host": arr_host,
        "apiKey": arr_api_key,
        "syncLevel": "fullSync",
        "prowlarrUrl": prowlarr_host,
    }
    if app_name.lower() == "whisparr":
        overrides["syncCategories"] = WHISPARR_SYNC_CATEGORIES
    fields = _build_fields_from_schema(schema, overrides)
    return {
        "name": name,
        "enable": True,
        "syncLevel": "fullSync",
        "implementation": schema.get("implementation"),
        "implementationName": schema.get("implementationName"),
        "configContract": schema.get("configContract"),
        "infoLink": schema.get("infoLink"),
        "tags": tag_ids or [],
        "fields": fields,
    }


def _get_prowlarr_tags(host: str, token: str) -> list[dict]:
    for path in ("/api/v1/tag", "/api/v3/tag"):
        try:
            data = _prowlarr_req(_join(host, path), token, "GET")
            if isinstance(data, list):
                return data
        except Exception:
            continue
    return []


def _ensure_tag_ids(
    host: str,
    token: str,
    labels: list[str],
    aliases: Optional[dict[str, str]] = None,
) -> dict[str, int]:
    tag_map = {}
    if not labels:
        return tag_map
    existing = _get_prowlarr_tags(host, token)
    for tag in existing:
        label = (tag.get("label") or "").strip().lower()
        tag_id = tag.get("id")
        if label and isinstance(tag_id, int):
            tag_map[label] = tag_id
    for label, alias in (aliases or {}).items():
        label_lc = str(label or "").strip().lower()
        alias_lc = str(alias or "").strip().lower()
        if label_lc and label_lc not in tag_map and alias_lc in tag_map:
            tag_map[label_lc] = tag_map[alias_lc]
    for label in labels:
        label_lc = label.strip().lower()
        if not label_lc or label_lc in tag_map:
            continue
        created = None
        for path in ("/api/v1/tag", "/api/v3/tag"):
            try:
                created = _prowlarr_req(
                    _join(host, path), token, "POST", {"label": label_lc}
                )
                break
            except Exception:
                continue
        if isinstance(created, dict):
            tag_id = created.get("id")
            if isinstance(tag_id, int):
                tag_map[label_lc] = tag_id
    return tag_map


def _uses_legacy_infinidysk_namespace() -> bool:
    checker = getattr(CONFIG_MANAGER, "uses_legacy_infinidysk_identity", None)
    if callable(checker):
        try:
            if checker():
                return True
        except Exception:
            pass
    config = getattr(CONFIG_MANAGER, "config", None)
    if not isinstance(config, dict):
        return False
    service = config.get("infinidysk") or config.get("nzbdav") or {}
    if not isinstance(service, dict):
        return False
    paths = [
        service.get("config_dir"),
        service.get("log_file"),
        *(service.get("symlink_backup_roots") or []),
    ]
    return any("nzbdav" in str(path or "").lower() for path in paths)


def _find_existing_application(
    existing: list, desired: dict, schema: dict, arr_host: str
) -> Optional[dict]:
    if not existing:
        return None
    name = (desired.get("name") or "").lower()
    match = next((c for c in existing if (c.get("name") or "").lower() == name), None)
    if match:
        return match
    impl = (schema.get("implementation") or "").lower()
    arr_host_norm = arr_host.rstrip("/")
    for item in existing:
        if (item.get("implementation") or "").lower() != impl:
            continue
        for field in item.get("fields") or []:
            if (field.get("name") or "").lower() == "baseurl":
                if (field.get("value") or "").rstrip("/") == arr_host_norm:
                    return item
    return None


def _find_existing_indexer(existing: list, desired: dict) -> Optional[dict]:
    if not existing:
        return None
    name = (desired.get("name") or "").lower()
    match = next((c for c in existing if (c.get("name") or "").lower() == name), None)
    if match:
        return match
    impl = (desired.get("implementation") or "").lower()
    for item in existing:
        if (item.get("implementation") or "").lower() == impl:
            return item
    return None


def _is_indexer_current(existing: dict, desired: dict) -> bool:
    if not existing or not desired:
        return False
    for key in ("enable", "implementation", "configContract"):
        if (existing.get(key) or "") != (desired.get(key) or ""):
            return False
    if not set(desired.get("tags") or []).issubset(existing.get("tags") or []):
        return False
    existing_fields = {
        (f.get("name") or "").lower(): f.get("value")
        for f in (existing.get("fields") or [])
    }
    desired_fields = {
        (f.get("name") or "").lower(): f.get("value")
        for f in (desired.get("fields") or [])
    }
    for key, value in desired_fields.items():
        if existing_fields.get(key) != value:
            return False
    return True


def _build_indexer_payload(
    schema: dict,
    indexer_name: str,
    base_url: str,
    tag_ids: Optional[list[int]] = None,
    enabled: bool = True,
) -> dict:
    definition_name = (schema.get("definitionName") or "").strip()
    definition_file = f"Custom/{definition_name}" if definition_name else ""
    overrides = {
        "baseUrl": base_url,
        "url": base_url,
        "host": base_url,
        "apiUrl": base_url,
        "indexerUrl": base_url,
        "link": base_url,
    }
    if definition_file:
        overrides["definitionFile"] = definition_file
    fields = _build_fields_from_schema(schema, overrides)
    protocol = (schema.get("protocol") or "").lower()
    payload = {
        "name": indexer_name,
        "enable": enabled,
        "priority": 25,
        "appProfileId": 0,
        "protocol": protocol or None,
        "implementation": schema.get("implementation"),
        "implementationName": schema.get("implementationName"),
        "configContract": schema.get("configContract"),
        "infoLink": schema.get("infoLink"),
        "minimumSeeders": 1,
        "definitionFile": definition_file or None,
        "tags": tag_ids or [],
        "fields": fields,
    }
    return payload


def _build_zilean_torznab_payload(
    schema: dict,
    base_url: str,
    tag_ids: Optional[list[int]] = None,
    enabled: bool = True,
) -> dict:
    torznab_url = f"{base_url.rstrip('/')}/torznab"
    overrides = {
        "baseUrl": torznab_url,
        "url": torznab_url,
        "host": torznab_url,
        "apiPath": "/api",
        "apiUrl": "/api",
        "apiKey": "",
    }
    fields = _build_fields_from_schema(schema, overrides)
    protocol = (schema.get("protocol") or "").lower()
    return {
        "name": "Zilean",
        "enable": enabled,
        "priority": 25,
        "appProfileId": 0,
        "protocol": protocol or "torrent",
        "implementation": schema.get("implementation"),
        "implementationName": schema.get("implementationName"),
        "configContract": schema.get("configContract"),
        "infoLink": schema.get("infoLink"),
        "minimumSeeders": 1,
        "definitionFile": None,
        "tags": tag_ids or [],
        "fields": fields,
    }


def _find_existing_zilean_indexer(existing: list, torznab_url: str) -> Optional[dict]:
    if not existing:
        return None
    for item in existing:
        if (item.get("name") or "").strip().lower() == "zilean":
            return item
    torznab_url = (torznab_url or "").rstrip("/").lower()
    for item in existing:
        fields = item.get("fields") or []
        for field in fields:
            name = (field.get("name") or "").lower()
            value = (field.get("value") or "").strip().rstrip("/").lower()
            if name in {"baseurl", "url", "host"} and value == torznab_url:
                return item
            if name == "definitionfile" and "custom/zilean" in value:
                return item
    return None


def _get_default_app_profile_id(host: str, token: str) -> Optional[int]:
    for path in ("/api/v1/appProfile", "/api/v1/appprofile", "/api/v3/appProfile"):
        try:
            data = _prowlarr_req(_join(host, path), token, "GET")
            if isinstance(data, list) and data:
                for item in data:
                    if isinstance(item, dict) and item.get("id"):
                        return int(item["id"])
        except Exception:
            continue
    return None


def _apply_indexer(host: str, token: str, desired: dict, match: Optional[dict]):
    if match:
        idx_id = match.get("id")
        payload = copy.deepcopy(desired)
        payload["id"] = idx_id
        _api_request(host, token, f"indexer/{idx_id}", "PUT", payload)
        return True, idx_id
    created = _api_request(host, token, "indexer", "POST", desired) or {}
    return bool(created.get("id")), created.get("id")


def _ensure_managed_indexer(host, token, desired, match, ownership, key):
    owned_id = ownership["indexers"].get(key)
    if match:
        if owned_id != match.get("id"):
            return (
                False,
                "Existing indexer is user-managed; configure or explicitly adopt it manually.",
            )
        if not _owned_row_is_current(ownership, f"indexer:{key}", match):
            return (
                False,
                "Indexer identity changed outside DUMB. Review its custom settings before applying managed changes.",
            )
        merged = copy.deepcopy(match)
        merged["tags"] = sorted(
            set(match.get("tags") or []) | set(desired.get("tags") or [])
        )
        if _is_indexer_current(match, merged):
            return True, "already_present"
        ok, saved_id = _apply_indexer(host, token, merged, match)
    else:
        ok, saved_id = _apply_indexer(host, token, desired, None)
    if ok and isinstance(saved_id, int):
        ownership["indexers"][key] = saved_id
        ownership.setdefault("signatures", {})[f"indexer:{key}"] = (
            _connection_fingerprint(merged if match else desired)
        )
    return ok, "applied" if ok else "Indexer creation returned no identity."


def ensure_zilean_indexer(
    host: str, token: str, base_url: str, tag_ids: list[int], ownership=None
):
    if ownership is None:
        return (
            False,
            "Prowlarr ownership metadata is required before automatic indexer setup.",
        )
    schema = _find_generic_torznab_schema(_get_prowlarr_indexer_schemas(host, token))
    if not schema:
        return False, "The installed Prowlarr API does not expose Generic Torznab."
    desired = _build_zilean_torznab_payload(schema, base_url, tag_ids)
    profile = _get_default_app_profile_id(host, token)
    if profile:
        desired["appProfileId"] = profile
    desired.pop("minimumSeeders", None)
    existing = _api_request(host, token, "indexer") or []
    saved_id = ownership["indexers"].get("zilean")
    match = next(
        (
            item
            for item in existing
            if saved_id is not None and item.get("id") == saved_id
        ),
        None,
    )
    match = match or _find_existing_zilean_indexer(
        existing, f"{base_url.rstrip('/')}/torznab"
    )
    return _ensure_managed_indexer(host, token, desired, match, ownership, "zilean")


def ensure_stremthru_indexer(host: str, token: str, tag_ids: list[int], ownership=None):
    if ownership is None:
        return (
            False,
            "Prowlarr ownership metadata is required before automatic indexer setup.",
        )
    schema = _find_indexer_schema(
        _get_prowlarr_indexer_schemas(host, token), "stremthru"
    )
    if not schema:
        return False, "The installed Prowlarr API does not expose StremThru."
    desired = _build_indexer_payload(
        schema,
        schema.get("name") or "Stremthru",
        "https://stremthru.13377001.xyz/v0/torznab",
        tag_ids,
    )
    profile = _get_default_app_profile_id(host, token)
    if profile:
        desired["appProfileId"] = profile
    existing = _api_request(host, token, "indexer") or []
    saved_id = ownership["indexers"].get("stremthru")
    match = next(
        (
            item
            for item in existing
            if saved_id is not None and item.get("id") == saved_id
        ),
        None,
    )
    if match is None:
        match = next(
            (
                item
                for item in existing
                if any(
                    str(field.get("name") or "").lower() == "definitionfile"
                    and "custom/stremthru" in str(field.get("value") or "").lower()
                    for field in item.get("fields") or []
                )
            ),
            None,
        )
    return _ensure_managed_indexer(host, token, desired, match, ownership, "stremthru")


def _apply_application(host: str, token: str, desired: dict, match: Optional[dict]):
    if match:
        app_id = match.get("id")
        payload = copy.deepcopy(desired)
        payload["id"] = app_id
        _api_request(host, token, f"applications/{app_id}", "PUT", payload)
        return True, app_id
    created = _api_request(host, token, "applications", "POST", desired) or {}
    return bool(created.get("id")), created.get("id")


def patch_prowlarr_apps(
    result_collector=None, *, configuration=None, target_ids=None
) -> Tuple[bool, Optional[str]]:
    token = _CONFIG_OVERRIDE.set(configuration)
    try:
        with _PROWLARR_PATCH_LOCK:
            return _patch_prowlarr_apps(result_collector, target_ids)
    finally:
        _CONFIG_OVERRIDE.reset(token)


def _patch_prowlarr_apps(
    result_collector=None, target_ids=None
) -> Tuple[bool, Optional[str]]:
    """Reconcile explicitly assigned owned links and retain actionable failures."""
    prowlarr_cfg = _setting("prowlarr") or {}
    enabled_instances = [
        (key, inst)
        for key, inst in (prowlarr_cfg.get("instances") or {}).items()
        if isinstance(inst, dict) and inst.get("enabled")
    ]
    arr_entries = _collect_arr_entries()
    errors = []
    for inst_key, inst in enabled_instances:

        def report(
            target, execution, detail, *, changed=False, verified=False, retryable=False
        ):
            emit_action(
                ActionResult(
                    action_id=f"prowlarr.link:{inst_key}->{target}",
                    service_key="prowlarr",
                    action="arr_link",
                    instance_key=inst_key,
                    execution=execution,
                    detail=detail,
                    changed=changed,
                    verification="passed" if verified else "not_checked",
                    retryable=retryable,
                ),
                result_collector,
            )
            if execution in {"failed", "waiting_dependency", "waiting_input"}:
                errors.append(f"Prowlarr {inst_key}: {target}: {detail}")

        selected = inst.get("arr_instances")
        all_ids = {f"{entry['service']}.{entry['instance']}" for entry in arr_entries}
        if selected is not None and (
            not isinstance(selected, list)
            or any(not isinstance(value, str) for value in selected)
        ):
            report(
                "selection",
                "waiting_input",
                "Arr assignments must be a list of canonical instance keys.",
            )
            continue
        if selected is not None:
            for unknown in set(selected) - all_ids:
                if unknown.startswith("external_arr."):
                    continue
                if target_ids is not None and unknown not in target_ids:
                    continue
                report(
                    unknown,
                    "waiting_input",
                    "The assigned Arr instance is missing or disabled. Update its assignment.",
                )
        entries = [
            entry
            for entry in arr_entries
            if selected is None or f"{entry['service']}.{entry['instance']}" in selected
        ]
        try:
            port = int(inst.get("port") or inst.get("host_port") or 0)
            if not 0 < port < 65536:
                report("readiness", "waiting_input", "Set a valid Prowlarr port.")
                continue
            host = _native_host(inst, port)
            cfg_path = (inst.get("config_file") or "").strip()
            token = _parse_arr_api_key(cfg_path)
            if not token:
                report(
                    "readiness",
                    "waiting_dependency",
                    "Waiting for the Prowlarr API key.",
                    retryable=True,
                )
                continue
            if not _wait_for_prowlarr(host, token):
                report(
                    "readiness",
                    "waiting_dependency",
                    "Waiting for the Prowlarr API.",
                    retryable=True,
                )
                continue
            schemas = _get_prowlarr_schemas(host, token)
            if not schemas:
                report(
                    "schema",
                    "waiting_dependency",
                    "Prowlarr application schemas are unavailable.",
                    retryable=True,
                )
                continue
            ownership = _load_ownership(inst)
            existing_apps = _api_request(host, token, "applications") or []
            workflow_labels = sorted(
                {
                    svc
                    for entry in entries
                    if target_ids is None
                    or f"{entry['service']}.{entry['instance']}" in target_ids
                    for svc in entry.get("core_services") or []
                    if svc in {"decypharr", "infinidysk", "altmount"}
                }
            )
            aliases = (
                {"infinidysk": "nzbdav"}
                if _uses_legacy_infinidysk_namespace()
                else None
            )
            tag_map = _ensure_tag_ids(host, token, workflow_labels, aliases)
            known_workflow_ids = {
                item["id"]
                for item in _get_prowlarr_tags(host, token)
                if isinstance(item, dict)
                and isinstance(item.get("id"), int)
                and str(item.get("label", "")).lower()
                in {"decypharr", "infinidysk", "nzbdav", "altmount"}
            }
            # Narrowing an explicit assignment retires only identifiable owned links.
            selected_ids = {
                f"{entry['service']}.{entry['instance']}" for entry in entries
            }
            selected_ids.update(
                identity
                for identity in (selected or [])
                if identity.startswith("external_arr.")
            )
            if selected is not None:
                for identity, owned_id in ownership["applications"].items():
                    if identity in selected_ids or (
                        target_ids is not None and identity not in target_ids
                    ):
                        continue
                    owned = next(
                        (app for app in existing_apps if app.get("id") == owned_id),
                        None,
                    )
                    if owned and not _owned_row_is_current(
                        ownership, f"app:{identity}", owned
                    ):
                        report(
                            identity,
                            "waiting_input",
                            "The saved connection identity changed. Review it before removing its assignment.",
                        )
                        continue
                    if owned and owned.get("enable", True):
                        retired = copy.deepcopy(owned)
                        retired["enable"] = False
                        retired["syncLevel"] = "disabled"
                        _apply_application(host, token, retired, owned)
                        report(
                            identity,
                            "applied",
                            "The owned connection was disabled after its assignment was removed.",
                            changed=True,
                        )
            if (
                target_ids is None
                and "decypharr" in workflow_labels
                and "decypharr" in tag_map
            ):
                zilean_cfg = _setting("zilean") or {}
                zilean_url = (
                    f"http://127.0.0.1:{int(zilean_cfg.get('port') or 8182)}"
                    if zilean_cfg.get("enabled")
                    else "https://zileanfortheweebs.midnightignite.me"
                )
                for name, ensure in (
                    (
                        "indexer.zilean",
                        lambda: ensure_zilean_indexer(
                            host, token, zilean_url, [tag_map["decypharr"]], ownership
                        ),
                    ),
                    (
                        "indexer.stremthru",
                        lambda: ensure_stremthru_indexer(
                            host, token, [tag_map["decypharr"]], ownership
                        ),
                    ),
                ):
                    try:
                        ok, outcome = ensure()
                        _save_ownership(inst, ownership)
                    except Exception:
                        report(
                            name,
                            "failed",
                            "Indexer configuration failed; check API compatibility and readiness.",
                            retryable=True,
                        )
                        continue
                    report(
                        name,
                        outcome if ok else "waiting_input",
                        (
                            "Indexer settings retained; credentials and custom URLs remain user-managed."
                            if ok
                            else outcome
                        ),
                        changed=ok and outcome == "applied",
                    )
            for entry in entries:
                identity = f"{entry['service']}.{entry['instance']}"
                if target_ids is not None and identity not in target_ids:
                    continue
                app_name = ARR_APP_MAP.get(
                    entry["service"], entry["service"].capitalize()
                )
                owned_id = ownership["applications"].get(identity)
                schema = _find_schema(schemas, app_name)
                if not schema:
                    report(
                        identity,
                        "waiting_input",
                        "The installed Prowlarr version does not expose this application's schema.",
                    )
                    continue
                if not entry.get("api_key"):
                    report(
                        identity,
                        "waiting_dependency",
                        "Waiting for the Arr API key.",
                        retryable=True,
                    )
                    continue
                required_labels = set(entry.get("core_services") or []) & {
                    "decypharr",
                    "infinidysk",
                    "altmount",
                }
                if not required_labels.issubset(tag_map):
                    report(
                        identity,
                        "failed",
                        "Workflow routing tags could not be established.",
                        retryable=True,
                    )
                    continue
                tags = [
                    tag_map[label]
                    for label in entry.get("core_services") or []
                    if label in tag_map
                ]
                desired = _build_application_payload(
                    schema,
                    app_name,
                    entry["host"],
                    entry["api_key"],
                    entry["instance"],
                    host,
                    sorted(set(tags)),
                )
                match = next(
                    (
                        app
                        for app in existing_apps
                        if owned_id is not None and app.get("id") == owned_id
                    ),
                    None,
                )
                conflict = _find_existing_application(
                    existing_apps, desired, schema, entry["host"]
                )
                if match is None and conflict:
                    report(
                        identity,
                        "waiting_input",
                        "An existing user-managed connection matches. Configure it manually or explicitly replace it.",
                    )
                    continue
                if match:
                    if not _owned_row_is_current(ownership, f"app:{identity}", match):
                        report(
                            identity,
                            "waiting_input",
                            "Connection identity changed outside DUMB. Review it before applying managed changes.",
                        )
                        continue
                    desired = _merge_application_payload(match, desired)
                    # Replace obsolete workflow routing tags, keeping ownership and user tags.
                    workflow_ids = known_workflow_ids
                    desired["tags"] = sorted(
                        (set(desired["tags"]) - workflow_ids) | set(tags)
                    )
                    if (
                        not match.get("enable", True)
                        or str(match.get("syncLevel") or "").lower() == "disabled"
                    ):
                        report(
                            identity,
                            "waiting_input",
                            "The managed application is disabled. Enable synchronization in Prowlarr when ready.",
                        )
                        continue
                api_version = "v1" if entry["service"] == "lidarr" else "v3"
                if not _wait_for_arr(
                    entry["host"],
                    entry["api_key"],
                    timeout_s=15,
                    interval_s=2,
                    api_version=api_version,
                ):
                    report(
                        identity,
                        "waiting_dependency",
                        "Waiting for the Arr API.",
                        retryable=True,
                    )
                    continue
                credential_digest = hashlib.sha256(
                    entry["api_key"].encode()
                ).hexdigest()
                known_credential = (ownership.get("credentials") or {}).get(
                    identity
                ) == credential_digest
                changed = not (
                    match
                    and _is_application_current(
                        match, desired, masked_credentials_current=known_credential
                    )
                    and set(match.get("tags") or []) == set(desired.get("tags") or [])
                    and match.get("name") == desired.get("name")
                )
                if changed:
                    ok, app_id = _apply_application(host, token, desired, match)
                    if not ok:
                        report(
                            identity,
                            "failed",
                            "Prowlarr did not return a saved application identity.",
                            retryable=True,
                        )
                        continue
                else:
                    app_id = match["id"]
                ownership["applications"][identity] = app_id
                ownership.setdefault("credentials", {})[identity] = credential_digest
                ownership.setdefault("signatures", {})[f"app:{identity}"] = (
                    _connection_fingerprint(desired)
                )
                _save_ownership(inst, ownership)
                readback = _api_request(host, token, f"applications/{app_id}")
                if (
                    not isinstance(readback, dict)
                    or not _is_application_current(
                        readback, desired, masked_credentials_current=True
                    )
                    or set(readback.get("tags") or []) != set(desired.get("tags") or [])
                    or readback.get("name") != desired.get("name")
                ):
                    report(
                        identity,
                        "failed",
                        "Prowlarr application readback did not match the intended connection.",
                        changed=changed,
                        retryable=True,
                    )
                    continue
                report(
                    identity,
                    "applied" if changed else "already_present",
                    "Connection configuration was read back. Test indexer synchronization in Prowlarr.",
                    changed=changed,
                    verified=False,
                )
        except Exception:
            report(
                "reconciliation",
                "failed",
                "Prowlarr reconciliation failed. Check service readiness and API compatibility.",
                retryable=True,
            )
            logger.warning("Prowlarr %s reconciliation failed.", inst_key)
    return (False, "; ".join(errors)) if errors else (True, None)
