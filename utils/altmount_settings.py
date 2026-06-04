from __future__ import annotations

import os
import shutil
import tarfile
import threading
import time
import zipfile
from typing import Any

import requests
import yaml

from utils.config_loader import CONFIG_MANAGER
from utils.core_services import has_core_service
from utils.decypharr_settings import (
    _parse_arr_api_key,
    _wait_for_arr,
    _with_retries,
    ensure_decypharr_sabnzbd_client,
)
from utils.download import Downloader
from utils.global_logger import logger
from utils.url_security import safe_request, safe_urlopen
from utils.versions import Versions

ALTMOUNT_MOUNT_TYPES = {"dfs", "rclone", "external_rclone", "none"}

_ALT_RETRY_LOCK = threading.Lock()
_ALT_RETRY_SCHEDULED = False
downloader = Downloader()
versions = Versions()


_CATEGORY_DEFAULTS = {
    "radarr": "movies",
    "sonarr": "tv",
    "lidarr": "music",
    "whisparr": "adult",
}


def _shutdown_requested() -> bool:
    try:
        from utils.dependencies import get_process_handler

        handler = get_process_handler()
    except Exception:
        return False
    return bool(getattr(handler, "shutting_down", False))


def _wait_for_altmount(port: int, timeout_s: int = 20, interval_s: float = 1.0) -> bool:
    deadline = time.time() + max(1, timeout_s)
    url = f"http://127.0.0.1:{int(port)}/live"
    while time.time() < deadline:
        if _shutdown_requested():
            return False
        try:
            req = safe_request(url, method="GET")
            with safe_urlopen(req, timeout=5) as resp:
                resp.read(1)
            return True
        except Exception:
            time.sleep(interval_s)
    return False


def _collect_arr_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for svc_name in ("sonarr", "radarr", "lidarr", "whisparr"):
        svc_cfg = CONFIG_MANAGER.get(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst_key, inst in instances.items():
            if not isinstance(inst, dict) or not inst.get("enabled"):
                continue
            if not has_core_service(inst, "altmount"):
                continue

            port = inst.get("port") or inst.get("host_port")
            try:
                port = str(int(port)) if port is not None else None
            except Exception:
                port = None
            if not port:
                continue

            cfg_path = (inst.get("config_file") or "").strip()
            token = _parse_arr_api_key(cfg_path)
            inst_name = (
                inst.get("instance_name") or inst.get("name") or inst_key or ""
            ).strip()
            label = f"{svc_name}:{inst_name}" if inst_name else svc_name
            entries.append(
                {
                    "service": svc_name,
                    "name": label,
                    "host": f"http://127.0.0.1:{port}",
                    "token": token,
                }
            )
    return entries


def _load_yaml(path: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    return loaded if isinstance(loaded, dict) else {}


def _ensure_category(categories: list[Any], name: str, order: int) -> None:
    for item in categories:
        if (
            isinstance(item, dict)
            and str(item.get("name") or "").lower() == name.lower()
        ):
            return
    categories.append({"name": name, "order": order, "priority": 0, "dir": name})


def _upsert_arr_instance(instances: list[Any], entry: dict[str, Any]) -> bool:
    desired = {
        "name": entry["name"],
        "url": entry["host"],
        "api_key": entry["token"],
        "enabled": True,
    }
    for item in instances:
        if not isinstance(item, dict):
            continue
        if item.get("name") == desired["name"] or item.get("url") == desired["url"]:
            changed = False
            for key, value in desired.items():
                if item.get(key) != value:
                    item[key] = value
                    changed = True
            return changed
    instances.append(desired)
    return True


def _normalize_mount_type(config: dict[str, Any]) -> str:
    mount_type = str(config.get("mount_type") or "").strip().lower()
    return mount_type if mount_type in ALTMOUNT_MOUNT_TYPES else "rclone"


def _altmount_config_mount_type(config: dict[str, Any]) -> str:
    mount_type = _normalize_mount_type(config)
    return {
        "dfs": "fuse",
        "rclone": "rclone",
        "external_rclone": "rclone_external",
        "none": "none",
    }[mount_type]


def _desired_rclone_config(config: dict[str, Any]) -> dict[str, Any]:
    config_dir = config.get("config_dir") or "/altmount"
    mount_type = _normalize_mount_type(config)
    return {
        "path": os.path.join(config_dir, "rclone"),
        "mount_enabled": mount_type == "rclone",
        "rc_enabled": mount_type in {"rclone", "external_rclone"},
        "rc_port": int(config.get("rclone_rc_port") or 5573),
        "rc_user": str(config.get("rclone_rc_user") or "admin"),
        "rc_pass": str(config.get("rclone_rc_pass") or "admin"),
    }


def download_altmount_binary(config: dict, target_bin: str) -> tuple[bool, str | None]:
    requested = versions.normalize_release_version(config.get("pinned_version"))
    if requested == "latest":
        latest_version, latest_error = downloader.get_latest_release(
            config.get("repo_owner", "javi11"),
            config.get("repo_name", "altmount"),
            nightly=False,
        )
        if not latest_version:
            return False, latest_error or "Failed to resolve latest AltMount release."
        requested = latest_version

    release_info, error = downloader.fetch_github_release_info(
        config.get("repo_owner", "javi11"),
        config.get("repo_name", "altmount"),
        requested,
    )
    if error or not release_info:
        return False, error or "Failed to fetch AltMount release information."

    release_tag = release_info.get("tag_name") or requested
    architecture = downloader.get_architecture()
    arch_asset_token = architecture.replace("-", "_")
    asset = None
    for candidate in release_info.get("assets", []):
        name = str(candidate.get("name") or "")
        lower_name = name.lower()
        if (
            lower_name.startswith("altmount-cli_")
            and arch_asset_token in lower_name
            and (lower_name.endswith(".tar.gz") or lower_name.endswith(".zip"))
        ):
            asset = candidate
            break
    if not asset:
        return False, f"No AltMount archive asset found for {architecture}."
    download_url = asset.get("browser_download_url")
    if not download_url:
        return False, "AltMount asset download URL missing."

    logger.info("Downloading AltMount %s from %s", release_tag, download_url)
    os.makedirs(os.path.dirname(target_bin), exist_ok=True)
    temp_archive = f"{target_bin}.download"
    temp_extract_dir = f"{target_bin}.extract"
    with requests.get(download_url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(temp_archive, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)

    if os.path.isdir(temp_extract_dir):
        shutil.rmtree(temp_extract_dir)
    os.makedirs(temp_extract_dir, exist_ok=True)
    try:
        asset_name = str(asset.get("name") or "").lower()
        extract_root = os.path.abspath(temp_extract_dir)

        def safe_target_path(member_name: str) -> str:
            target_path = os.path.abspath(os.path.join(extract_root, member_name))
            if not target_path.startswith(extract_root + os.sep):
                raise ValueError(f"Unsafe AltMount archive member: {member_name}")
            return target_path

        if asset_name.endswith(".zip"):
            with zipfile.ZipFile(temp_archive) as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    mode = (member.external_attr >> 16) & 0o170000
                    if mode == 0o120000:
                        continue
                    target_path = safe_target_path(member.filename)
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    with (
                        archive.open(member) as source,
                        open(target_path, "wb") as dest,
                    ):
                        shutil.copyfileobj(source, dest)
        else:
            with tarfile.open(temp_archive, "r:gz") as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    target_path = safe_target_path(member.name)
                    source = archive.extractfile(member)
                    if source is None:
                        continue
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    with source, open(target_path, "wb") as dest:
                        shutil.copyfileobj(source, dest)

        binary_source = None
        for root, _, files in os.walk(temp_extract_dir):
            for filename in files:
                normalized_filename = filename.lower()
                if normalized_filename in {
                    "altmount",
                    "altmount-cli",
                } or normalized_filename.startswith("altmount-cli-"):
                    binary_source = os.path.join(root, filename)
                    break
            if binary_source:
                break
        if not binary_source:
            return False, "AltMount archive did not contain an altmount executable."
        shutil.copy2(binary_source, target_bin)
    finally:
        try:
            os.remove(temp_archive)
        except OSError:
            pass
        shutil.rmtree(temp_extract_dir, ignore_errors=True)

    os.chmod(target_bin, 0o755)
    with open(
        versions.version_marker_path(os.path.dirname(target_bin)),
        "w",
        encoding="utf-8",
    ) as handle:
        handle.write(str(release_tag))
    return True, None


def write_altmount_default_config(config: dict) -> None:
    config_file = config.get("config_file") or os.path.join(
        config.get("config_dir", "/altmount"), "config.yaml"
    )
    if os.path.isfile(config_file):
        return

    config_dir = config.get("config_dir") or "/altmount"
    metadata_dir = config.get("metadata_dir") or os.path.join(config_dir, "metadata")
    mount_path = config.get("mount_path") or "/mnt/debrid/altmount"
    log_file = config.get("log_file") or os.path.join(
        config_dir, "logs", "altmount.log"
    )
    port = int(config.get("port") or 8088)
    log_level = str(config.get("log_level") or "info").lower()
    mount_type = _altmount_config_mount_type(config)
    rclone_cfg = _desired_rclone_config(config)
    os.makedirs(os.path.dirname(config_file), exist_ok=True)
    rendered = f"""# Generated by DUMB. Edit this file or use the AltMount UI for provider and workflow settings.
webdav:
  port: {port}
  user: usenet
  password: usenet
  host: ''
api:
  prefix: /api
  key_override: {str((config.get('env') or {}).get('ALTMOUNT_API_KEY') or '')}
auth:
  login_required: true
database:
  path: {os.path.join(config_dir, 'altmount.db')}
metadata:
  root_path: {metadata_dir}
  delete_source_nzb_on_removal: false
  delete_completed_nzb: false
mount_path: {mount_path}
mount_type: {mount_type}
rclone:
  path: {rclone_cfg['path']}
  mount_enabled: {str(rclone_cfg['mount_enabled']).lower()}
  rc_enabled: {str(rclone_cfg['rc_enabled']).lower()}
  rc_port: {rclone_cfg['rc_port']}
  rc_user: {rclone_cfg['rc_user']}
  rc_pass: {rclone_cfg['rc_pass']}
sabnzbd:
  enabled: true
  complete_dir: /
  categories:
    - name: Default
      order: 0
      priority: 0
      dir: complete
    - name: movies
      order: 1
      priority: 0
      dir: movies
    - name: tv
      order: 2
      priority: 0
      dir: tv
    - name: music
      order: 3
      priority: 0
      dir: music
    - name: adult
      order: 4
      priority: 0
      dir: adult
arrs:
  enabled: true
  radarr_instances: []
  sonarr_instances: []
  lidarr_instances: []
  whisparr_instances: []
  queue_cleanup_enabled: true
  queue_cleanup_interval_seconds: 300
log:
  file: {log_file}
  level: {log_level}
  max_size: 100
  max_age: 30
  max_backups: 10
  compress: true
log_level: {log_level}
providers: []
"""
    with open(config_file, "w", encoding="utf-8") as handle:
        handle.write(rendered)


def sync_altmount_managed_config(config: dict) -> None:
    config_file = config.get("config_file") or os.path.join(
        config.get("config_dir", "/altmount"), "config.yaml"
    )
    if not os.path.isfile(config_file):
        return

    try:
        data = _load_yaml(config_file)
    except Exception as exc:
        logger.warning("Could not read AltMount config for managed sync: %s", exc)
        return

    changed = False
    mount_path = config.get("mount_path") or "/mnt/debrid/altmount"
    if data.get("mount_path") != mount_path:
        data["mount_path"] = mount_path
        changed = True

    mount_type = _altmount_config_mount_type(config)
    if data.get("mount_type") != mount_type:
        data["mount_type"] = mount_type
        changed = True

    rclone_cfg = data.setdefault("rclone", {})
    if not isinstance(rclone_cfg, dict):
        rclone_cfg = {}
        data["rclone"] = rclone_cfg
    for key, value in _desired_rclone_config(config).items():
        if rclone_cfg.get(key) != value:
            rclone_cfg[key] = value
            changed = True

    api_key = str((config.get("env") or {}).get("ALTMOUNT_API_KEY") or "").strip()
    api_cfg = data.setdefault("api", {})
    if not isinstance(api_cfg, dict):
        api_cfg = {}
        data["api"] = api_cfg
    if api_cfg.get("prefix") != "/api":
        api_cfg["prefix"] = "/api"
        changed = True
    if api_key and api_cfg.get("key_override") != api_key:
        api_cfg["key_override"] = api_key
        changed = True

    sab_cfg = data.setdefault("sabnzbd", {})
    if not isinstance(sab_cfg, dict):
        sab_cfg = {}
        data["sabnzbd"] = sab_cfg
    if sab_cfg.get("enabled") is not True:
        sab_cfg["enabled"] = True
        changed = True
    sab_cfg.setdefault("complete_dir", "/")
    categories = sab_cfg.setdefault("categories", [])
    if not isinstance(categories, list):
        categories = []
        sab_cfg["categories"] = categories
        changed = True
    existing = {
        str(item.get("name") or "").lower()
        for item in categories
        if isinstance(item, dict)
    }
    for order, name in enumerate(("Default", "movies", "tv", "music", "adult")):
        if name.lower() not in existing:
            categories.append(
                {
                    "name": name,
                    "order": order,
                    "priority": 0,
                    "dir": "complete" if name == "Default" else name,
                }
            )
            changed = True

    arrs_cfg = data.setdefault("arrs", {})
    if not isinstance(arrs_cfg, dict):
        arrs_cfg = {}
        data["arrs"] = arrs_cfg
    if arrs_cfg.get("enabled") is not True:
        arrs_cfg["enabled"] = True
        changed = True
    for field in (
        "radarr_instances",
        "sonarr_instances",
        "lidarr_instances",
        "whisparr_instances",
    ):
        if not isinstance(arrs_cfg.get(field), list):
            arrs_cfg[field] = []
            changed = True
    arrs_cfg.setdefault("queue_cleanup_enabled", True)
    arrs_cfg.setdefault("queue_cleanup_interval_seconds", 300)

    if changed:
        with open(config_file, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False)


def _sync_altmount_config(
    config: dict[str, Any], arr_entries: list[dict[str, Any]]
) -> None:
    config_file = config.get("config_file") or os.path.join(
        config.get("config_dir", "/altmount"), "config.yaml"
    )
    api_key = str((config.get("env") or {}).get("ALTMOUNT_API_KEY") or "").strip()
    if not api_key:
        logger.warning("AltMount API key missing; skipping AltMount config sync.")
        return

    data = _load_yaml(config_file)
    changed = False

    api_cfg = data.setdefault("api", {})
    if not isinstance(api_cfg, dict):
        api_cfg = {}
        data["api"] = api_cfg
    if api_cfg.get("prefix") != "/api":
        api_cfg["prefix"] = "/api"
        changed = True
    if api_cfg.get("key_override") != api_key:
        api_cfg["key_override"] = api_key
        changed = True

    sab_cfg = data.setdefault("sabnzbd", {})
    if not isinstance(sab_cfg, dict):
        sab_cfg = {}
        data["sabnzbd"] = sab_cfg
    if sab_cfg.get("enabled") is not True:
        sab_cfg["enabled"] = True
        changed = True
    sab_cfg.setdefault("complete_dir", "/")
    categories = sab_cfg.setdefault("categories", [])
    if not isinstance(categories, list):
        categories = []
        sab_cfg["categories"] = categories
        changed = True
    before_len = len(categories)
    _ensure_category(categories, "Default", 0)
    for idx, name in enumerate(("movies", "tv", "music", "adult"), start=1):
        _ensure_category(categories, name, idx)
    if len(categories) != before_len:
        changed = True

    arrs_cfg = data.setdefault("arrs", {})
    if not isinstance(arrs_cfg, dict):
        arrs_cfg = {}
        data["arrs"] = arrs_cfg
    if arrs_cfg.get("enabled") is not True:
        arrs_cfg["enabled"] = True
        changed = True
    for field in (
        "radarr_instances",
        "sonarr_instances",
        "lidarr_instances",
        "whisparr_instances",
    ):
        if not isinstance(arrs_cfg.get(field), list):
            arrs_cfg[field] = []
            changed = True
    for entry in arr_entries:
        field = f"{entry['service']}_instances"
        if _upsert_arr_instance(arrs_cfg[field], entry):
            changed = True

    if changed:
        os.makedirs(os.path.dirname(config_file), exist_ok=True)
        with open(config_file, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False)
        logger.info("Updated AltMount Arr integration config at %s", config_file)


def _schedule_altmount_retry(delay_s: int = 15) -> None:
    global _ALT_RETRY_SCHEDULED
    with _ALT_RETRY_LOCK:
        if _ALT_RETRY_SCHEDULED:
            return
        _ALT_RETRY_SCHEDULED = True

    def _runner():
        global _ALT_RETRY_SCHEDULED
        try:
            time.sleep(max(1, delay_s))
            patch_altmount_arr_integration()
        finally:
            with _ALT_RETRY_LOCK:
                _ALT_RETRY_SCHEDULED = False

    threading.Thread(target=_runner, daemon=True).start()


def patch_altmount_arr_integration() -> tuple[bool, str | None]:
    if _shutdown_requested():
        return False, "Shutdown requested"

    config = CONFIG_MANAGER.get("altmount") or {}
    if not config.get("enabled"):
        return True, None

    arr_entries = _collect_arr_entries()
    if not arr_entries:
        logger.debug("No Arr instances linked to AltMount; skipping Arr automation.")
        return True, None

    _sync_altmount_config(config, arr_entries)

    api_key = str((config.get("env") or {}).get("ALTMOUNT_API_KEY") or "").strip()
    if not api_key:
        return False, "AltMount API key missing"

    port = int(config.get("port") or 8088)
    if not _wait_for_altmount(port, timeout_s=20, interval_s=1.0):
        logger.warning(
            "AltMount backend not reachable on 127.0.0.1:%s; scheduling Arr setup retry.",
            port,
        )
        _schedule_altmount_retry()
        return False, "AltMount backend not reachable"

    for entry in arr_entries:
        host = entry.get("host") or ""
        token = entry.get("token") or ""
        service = entry.get("service") or ""
        api_version = "v1" if service == "lidarr" else "v3"
        if not token:
            logger.warning(
                "%s API token missing; skipping AltMount download client.", host
            )
            continue
        if not _wait_for_arr(
            host, token, timeout_s=60, interval_s=2.0, api_version=api_version
        ):
            logger.warning(
                "Arr not up yet, skipping AltMount download client ensure for %s", host
            )
            continue
        _with_retries(
            ensure_decypharr_sabnzbd_client,
            arr_host=host,
            arr_api_key=token,
            decypharr_port=port,
            api_token=api_key,
            service=service,
            name="altmount",
            category=entry.get("name"),
            category_map=_CATEGORY_DEFAULTS,
            test_before_save=False,
            api_version=api_version,
            attempts=3,
        )
    return True, None
