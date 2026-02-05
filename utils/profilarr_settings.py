import json
import logging
import os
import sqlite3
import subprocess
from pathlib import Path

from utils.config_loader import CONFIG_MANAGER
from utils.core_services import get_core_services
from utils.decypharr_settings import _parse_arr_api_key
from utils.user_management import chown_recursive, chown_single

logger = logging.getLogger(__name__)

DEFAULT_PROFILARR_REPO = "https://github.com/johman10/profilarr-trash-guides"


def _normalize_arr_name(instance_key: str, instance: dict, svc_name: str) -> str:
    label = (
        instance.get("instance_name")
        or instance.get("name")
        or instance.get("process_name")
        or instance_key
        or svc_name
    )
    return f"{svc_name}:{label}"


def _build_arr_entries(core_services: list[str]) -> list[dict]:
    entries = []
    for svc_name in ("sonarr", "radarr"):
        svc_cfg = CONFIG_MANAGER.get(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst_key, inst in instances.items():
            if not isinstance(inst, dict) or not inst.get("enabled"):
                continue
            if not inst.get("use_profilarr"):
                continue
            inst_core = get_core_services(inst)
            if core_services and not any(cs in inst_core for cs in core_services):
                continue

            port = inst.get("port") or inst.get("host_port")
            try:
                port = str(int(port)) if port is not None else None
            except Exception:
                port = None
            if not port:
                logger.warning("Profilarr auto-link: missing port for %s %s", svc_name, inst_key)
                continue

            api_key = _parse_arr_api_key(inst.get("config_file", ""))
            if not api_key:
                logger.warning("Profilarr auto-link: missing API key for %s %s", svc_name, inst_key)
                continue

            entry = {
                "name": _normalize_arr_name(inst_key, inst, svc_name),
                "type": svc_name,
                "tags": [
                    "dumb:auto",
                    svc_name.capitalize(),
                    *([f"core_service:{cs}" for cs in core_services] if core_services else []),
                ],
                "arr_server": f"http://127.0.0.1:{port}",
                "api_key": api_key,
                "data_to_sync": json.dumps({"profiles": [], "customFormats": []}),
                "sync_method": "manual",
                "sync_interval": 0,
                "import_as_unique": False,
            }
            entries.append(entry)
    return entries


def _list_repo_items(config_root: str) -> dict:
    repo_root = os.path.join(config_root, "db")
    profiles_dir = os.path.join(repo_root, "profiles")
    formats_dir = os.path.join(repo_root, "custom_formats")
    regex_dir = os.path.join(repo_root, "regex_patterns")
    media_dir = os.path.join(repo_root, "media_management")

    def _names_from(path: str) -> list[str]:
        if not os.path.isdir(path):
            return []
        names = []
        for item in Path(path).glob("*.yml"):
            names.append(item.stem)
        return sorted(set(names))

    result = {
        "profiles": _names_from(profiles_dir),
        "customFormats": _names_from(formats_dir),
        "regexPatterns": _names_from(regex_dir),
        "mediaManagement": _names_from(media_dir),
    }
    logger.debug(
        "Profilarr repo items: %s profiles, %s custom formats, %s regex patterns, %s media management",
        len(result["profiles"]),
        len(result["customFormats"]),
        len(result["regexPatterns"]),
        len(result["mediaManagement"]),
    )
    return result


def _filter_repo_items_by_type(repo_items: dict, arr_type: str) -> dict:
    arr_key = arr_type.lower()
    filtered = {
        "profiles": [],
        "customFormats": [],
        "regexPatterns": [],
        "mediaManagement": [],
    }
    if not repo_items:
        return filtered

    for key in ("profiles", "customFormats"):
        items = repo_items.get(key) or []
        filtered_items = [item for item in items if arr_key in item.lower()]
        filtered[key] = filtered_items

    for key in ("regexPatterns", "mediaManagement"):
        items = repo_items.get(key) or []
        if not items:
            filtered[key] = []
            continue
        tagged = [item for item in items if arr_key in item.lower()]
        filtered[key] = tagged if tagged else list(items)

    if any(
        repo_items.get(key)
        for key in ("profiles", "customFormats", "regexPatterns", "mediaManagement")
    ) and not any(
        filtered.get(key)
        for key in ("profiles", "customFormats", "regexPatterns", "mediaManagement")
    ):
        logger.warning(
            "Profilarr repo items not tagged for %s; skipping auto sync seed for this app.",
            arr_type,
        )
    return filtered


def _ensure_profilarr_db(backend_dir: str, config_root: str) -> tuple[bool, str | None]:
    python_bin = os.path.join(backend_dir, "venv", "bin", "python")
    if not os.path.isfile(python_bin):
        return False, "Profilarr venv python not found"
    env = os.environ.copy()
    env["PYTHONPATH"] = backend_dir
    env["PROFILARR_CONFIG_DIR"] = config_root
    cmd = [
        python_bin,
        "-c",
        "from app.db.migrations.runner import run_migrations; run_migrations()",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip()
    except Exception as exc:
        return False, str(exc)
    return True, None


def _ensure_default_repo(
    backend_dir: str, config_root: str, repo_url: str = DEFAULT_PROFILARR_REPO
) -> tuple[bool, str | None]:
    repo_path = os.path.join(config_root, "db")
    if os.path.isdir(os.path.join(repo_path, ".git")):
        logger.debug("Profilarr repo already initialized at %s", repo_path)
        return True, None
    if not repo_url:
        logger.debug("Profilarr default repo URL not set; skipping.")
        return True, None

    logger.info("Profilarr default repo not detected. Seeding %s", repo_url)

    python_bin = os.path.join(backend_dir, "venv", "bin", "python")
    if not os.path.isfile(python_bin):
        return False, "Profilarr venv python not found"

    env = os.environ.copy()
    env["PYTHONPATH"] = backend_dir
    env["PROFILARR_CONFIG_DIR"] = config_root

    snippet = (
        "from app.db.queries.settings import get_settings, save_settings\n"
        "from app.config import config as config_mod\n"
        "from app.git.repo.clone import clone_repository\n"
        f"repo_url = {repo_url!r}\n"
        "settings = get_settings() or {}\n"
        "current = settings.get('gitRepo') or ''\n"
        "if current.strip():\n"
        "    raise SystemExit(0)\n"
        "save_settings({'gitRepo': repo_url})\n"
        "ok, msg = clone_repository(repo_url, config_mod.DB_DIR)\n"
        "raise SystemExit(0 if ok else msg)\n"
    )

    try:
        result = subprocess.run(
            [python_bin, "-c", snippet], capture_output=True, text=True, env=env
        )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            logger.error("Profilarr default repo clone failed: %s", err)
            return False, err or "Profilarr default repo clone failed"
    except Exception as exc:
        return False, str(exc)

    logger.info("Profilarr default repo seeded successfully.")
    return True, None


def _run_initial_sync(backend_dir: str, config_root: str, arr_ids: list[int]) -> None:
    if not arr_ids:
        return
    logger.info("Profilarr initial sync starting for %s arr configs", len(arr_ids))
    python_bin = os.path.join(backend_dir, "venv", "bin", "python")
    if not os.path.isfile(python_bin):
        logger.warning("Profilarr venv python not found for initial sync.")
        return

    env = os.environ.copy()
    env["PYTHONPATH"] = backend_dir
    env["PROFILARR_CONFIG_DIR"] = config_root

    id_list = ",".join(str(i) for i in arr_ids)
    snippet = (
        "from app.importer import handle_pull_import\n"
        "from app.arr.manager import get_arr_config\n"
        "from app.media_management import get_media_management_data\n"
        "from app.media_management.sync import sync_naming_config, sync_media_management_config, sync_quality_definitions\n"
        f"ids = [{id_list}]\n"
        "for _id in ids:\n"
        "    handle_pull_import(_id)\n"
        "    arr_result = get_arr_config(_id)\n"
        "    if not arr_result.get('success'):\n"
        "        continue\n"
        "    arr_cfg = arr_result.get('data') or {}\n"
        "    base_url = arr_cfg.get('arrServer')\n"
        "    api_key = arr_cfg.get('apiKey')\n"
        "    arr_type = arr_cfg.get('type')\n"
        "    if not base_url or not api_key or not arr_type:\n"
        "        continue\n"
        "    naming = get_media_management_data('naming')\n"
        "    misc = get_media_management_data('misc')\n"
        "    quality_defs = get_media_management_data('quality_definitions')\n"
        "    sync_naming_config(base_url, api_key, arr_type, (naming.get(arr_type) or {}))\n"
        "    sync_media_management_config(base_url, api_key, arr_type, (misc.get(arr_type) or {}))\n"
        "    q_defs = (quality_defs.get('qualityDefinitions') or {}).get(arr_type, {})\n"
        "    sync_quality_definitions(base_url, api_key, arr_type, q_defs)\n"
    )

    try:
        result = subprocess.run([python_bin, "-c", snippet], env=env, check=False, capture_output=True, text=True)
        if result.stdout:
            logger.debug("Profilarr initial sync output: %s", result.stdout.strip())
        if result.stderr:
            logger.warning("Profilarr initial sync stderr: %s", result.stderr.strip())
        logger.info("Profilarr initial sync completed for %s arr configs", len(arr_ids))
    except Exception as exc:
        logger.warning("Profilarr initial sync failed: %s", exc)


def sync_profilarr_arr_configs(profilarr_instance: dict) -> tuple[bool, str | None]:
    if not isinstance(profilarr_instance, dict):
        return False, "Profilarr instance config missing"

    core_services = get_core_services(profilarr_instance)
    if not core_services:
        logger.info("Profilarr core_service is blank; skipping auto-link (manual mode).")
        return True, None

    config_root = os.path.join(profilarr_instance.get("config_dir", "/profilarr/default"), "config")
    backend_dir = os.path.join(profilarr_instance.get("config_dir", "/profilarr/default"), "backend")
    repo_path = os.path.join(config_root, "db")
    user_id = CONFIG_MANAGER.get("puid")
    group_id = CONFIG_MANAGER.get("pgid")

    _ensure_config_ownership(config_root, user_id, group_id)
    _ensure_repo_ownership(repo_path, user_id, group_id)

    ok, err = _ensure_profilarr_db(backend_dir, config_root)
    if not ok:
        return False, f"Profilarr DB migration failed: {err}"

    ok, err = _ensure_default_repo(backend_dir, config_root)
    if not ok:
        logger.warning("Profilarr default repo setup failed: %s", err)
    else:
        _ensure_config_ownership(config_root, user_id, group_id)
        _ensure_repo_ownership(repo_path, user_id, group_id)

    db_path = os.path.join(config_root, "profilarr.db")
    if not os.path.isfile(db_path):
        return False, "Profilarr DB not found after migration"

    entries = _build_arr_entries(core_services)
    if not entries:
        logger.info("Profilarr auto-link: no eligible Arr instances found.")
        return True, None

    logger.debug("Profilarr auto-link found %s Arr entries", len(entries))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        existing = cursor.execute(
            "SELECT id, name, tags FROM arr_config"
        ).fetchall()
        managed = {
            row["name"]
            for row in existing
            if row["tags"] and "dumb:auto" in (json.loads(row["tags"]) or [])
        }

        desired_names = {entry["name"] for entry in entries}

        inserted_ids = []
        repo_data = _list_repo_items(config_root)
        for entry in entries:
            row = cursor.execute(
                "SELECT id FROM arr_config WHERE name = ?",
                (entry["name"],),
            ).fetchone()
            if row:
                logger.debug("Profilarr auto-link updating arr_config for %s", entry["name"])
                entry_repo_data = _filter_repo_items_by_type(repo_data, entry["type"])
                entry_repo_data_json = json.dumps(entry_repo_data)
                existing_sync = cursor.execute(
                    "SELECT data_to_sync FROM arr_config WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                if existing_sync and existing_sync["data_to_sync"]:
                    try:
                        existing_data = json.loads(existing_sync["data_to_sync"])
                    except Exception:
                        existing_data = {}
                else:
                    existing_data = {}
                merged = dict(existing_data or {})
                for key, items in entry_repo_data.items():
                    if key not in merged:
                        merged[key] = items
                entry_repo_data_json = json.dumps(merged)
                cursor.execute(
                    """
                    UPDATE arr_config
                    SET type = ?, tags = ?, arr_server = ?, api_key = ?,
                        data_to_sync = ?,
                        sync_method = COALESCE(sync_method, ?),
                        sync_interval = COALESCE(sync_interval, ?),
                        import_as_unique = COALESCE(import_as_unique, ?)
                    WHERE id = ?
                    """,
                    (
                        entry["type"],
                        json.dumps(entry["tags"]),
                        entry["arr_server"],
                        entry["api_key"],
                        entry_repo_data_json,
                        entry["sync_method"],
                        entry["sync_interval"],
                        int(entry["import_as_unique"]),
                        row["id"],
                    ),
                )
            else:
                logger.info("Profilarr auto-link creating arr_config for %s", entry["name"])
                entry_repo_data = _filter_repo_items_by_type(repo_data, entry["type"])
                entry["data_to_sync"] = json.dumps(entry_repo_data)
                cursor.execute(
                    """
                    INSERT INTO arr_config
                    (name, type, tags, arr_server, api_key, data_to_sync,
                     sync_method, sync_interval, import_as_unique)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry["name"],
                        entry["type"],
                        json.dumps(entry["tags"]),
                        entry["arr_server"],
                        entry["api_key"],
                        entry["data_to_sync"],
                        entry["sync_method"],
                        entry["sync_interval"],
                        int(entry["import_as_unique"]),
                    ),
                )
                inserted_ids.append(cursor.lastrowid)

        # Remove stale managed entries
        stale = managed - desired_names
        if stale:
            logger.info("Profilarr auto-link removing %s stale entries", len(stale))
            cursor.execute(
                "DELETE FROM arr_config WHERE name IN ({})".format(
                    ",".join("?" for _ in stale)
                ),
                tuple(stale),
            )

        conn.commit()

    _run_initial_sync(backend_dir, config_root, inserted_ids)
    logger.info("Profilarr auto-link updated %s arr configs.", len(entries))
    return True, None


def _ensure_repo_ownership(repo_path: str, user_id: int | None, group_id: int | None) -> None:
    if not repo_path or user_id is None or group_id is None:
        return
    try:
        stat_info = os.stat(repo_path)
    except FileNotFoundError:
        return
    except Exception as exc:
        logger.debug("Profilarr repo ownership check failed for %s: %s", repo_path, exc)
        return

    if stat_info.st_uid == user_id and stat_info.st_gid == group_id:
        return

    logger.info(
        "Profilarr repo ownership mismatch at %s (uid=%s gid=%s). Fixing to %s:%s",
        repo_path,
        stat_info.st_uid,
        stat_info.st_gid,
        user_id,
        group_id,
    )
    ok, err = chown_recursive(repo_path, user_id, group_id)
    if not ok:
        logger.warning("Profilarr repo chown failed for %s: %s", repo_path, err)


def _ensure_config_ownership(config_root: str, user_id: int | None, group_id: int | None) -> None:
    if not config_root or user_id is None or group_id is None:
        return
    db_path = os.path.join(config_root, "profilarr.db")
    try:
        chown_single(config_root, user_id, group_id)
    except Exception as exc:
        logger.debug("Profilarr config chown failed for %s: %s", config_root, exc)
    try:
        chown_single(db_path, user_id, group_id)
    except Exception:
        pass
    if os.path.isdir(os.path.join(config_root, "db")):
        ok, err = chown_recursive(os.path.join(config_root, "db"), user_id, group_id)
        if not ok:
            logger.warning("Profilarr config db chown failed: %s", err)


def any_arr_uses_profilarr() -> bool:
    for svc_name in ("sonarr", "radarr"):
        svc_cfg = CONFIG_MANAGER.get(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst in instances.values():
            if not isinstance(inst, dict) or not inst.get("enabled"):
                continue
            if inst.get("use_profilarr"):
                return True
    return False


def patch_profilarr_config() -> tuple[bool, str | None]:
    profilarr_cfg = CONFIG_MANAGER.get("profilarr") or {}
    instances = (profilarr_cfg.get("instances") or {}) or {}
    if not isinstance(instances, dict) or not instances:
        return True, None

    errors = []
    for inst in instances.values():
        if not isinstance(inst, dict) or not inst.get("enabled"):
            continue
        ok, err = sync_profilarr_arr_configs(inst)
        if not ok:
            errors.append(err or "Unknown error")
    if errors:
        return False, "; ".join(errors)
    return True, None
