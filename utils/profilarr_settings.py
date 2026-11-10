from contextvars import ContextVar
import json
import logging
import os
import re
import sqlite3
import subprocess
from pathlib import Path

from utils.config_loader import CONFIG_MANAGER
from utils.core_services import get_core_services
from utils.decypharr_settings import _parse_arr_api_key
from utils.user_management import chown_recursive, chown_single
from utils.versions import PROFILARR_LEGACY_RELEASE_VERSION
from utils.integration_result import ActionResult, emit_action
import defusedxml.ElementTree as ET

logger = logging.getLogger(__name__)


_CONFIG_OVERRIDE = ContextVar("arr_settings_configuration", default=None)


def _setting(key, default=None):
    configuration = _CONFIG_OVERRIDE.get()
    return (
        configuration.get(key, default)
        if configuration is not None
        else CONFIG_MANAGER.get(key, default)
    )


DEFAULT_PROFILARR_REPO = "https://github.com/johman10/profilarr-trash-guides"
PROFILARR_V2_DENO_MANIFESTS = ("deno.json", "deno.jsonc")
PROFILARR_V2_LAYOUT_FILES = ("vite.config.ts",)


def profilarr_v2_runtime_environment(
    config_root: str,
    port: str | int,
    sqlite_library: str | None = None,
) -> dict[str, str]:
    """Return a writable, persistent runtime environment for Profilarr v2."""
    environment = {
        "APP_BASE_PATH": config_root,
        "PORT": str(port),
        "HOST": "0.0.0.0",
        "HOME": config_root,
        "XDG_CACHE_HOME": os.path.join(config_root, ".cache"),
        "DENO_DIR": os.path.join(config_root, ".deno"),
        "DENO_NO_UPDATE_CHECK": "1",
    }
    if sqlite_library:
        environment["DENO_SQLITE_PATH"] = sqlite_library
    return environment


def detect_profilarr_layout(config_dir: str) -> str | None:
    """Return the installed Profilarr generation from its source/runtime layout."""
    if os.path.isfile(os.path.join(config_dir, "backend", "app", "main.py")):
        return "v1"
    if os.path.isfile(os.path.join(config_dir, "runtime", "profilarr")):
        return "v2"
    has_deno_manifest = any(
        os.path.isfile(os.path.join(config_dir, filename))
        for filename in PROFILARR_V2_DENO_MANIFESTS
    )
    if (
        has_deno_manifest
        and all(
            os.path.isfile(os.path.join(config_dir, filename))
            for filename in PROFILARR_V2_LAYOUT_FILES
        )
        and os.path.isdir(os.path.join(config_dir, "src"))
    ):
        return "v2"
    return None


def profilarr_deno_version(config_dir: str) -> str:
    """Return the Deno builder version declared by Profilarr's Dockerfile."""
    dockerfile = os.path.join(config_dir, "Dockerfile")
    try:
        with open(dockerfile, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError as exc:
        raise RuntimeError(f"Unable to read Profilarr v2 Dockerfile: {exc}") from exc

    match = re.search(
        r"^FROM\s+denoland/deno:(\S+)\s+AS\s+builder\s*$",
        content,
        re.I | re.M,
    )
    if not match:
        raise RuntimeError(
            "Profilarr v2 does not declare a supported Deno builder version."
        )

    image_tag = match.group(1)
    if re.fullmatch(r"\d+\.\d+\.\d+", image_tag):
        return image_tag

    arg_reference = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", image_tag)
    if arg_reference:
        arg_name = re.escape(arg_reference.group(1))
        arg_match = re.search(
            rf"^ARG\s+{arg_name}=(\d+\.\d+\.\d+)\s*$",
            content,
            re.M,
        )
        if arg_match:
            return arg_match.group(1)

    raise RuntimeError(
        "Profilarr v2 does not declare a supported Deno builder version."
    )


def profilarr_runtime_ready(config_dir: str) -> bool:
    layout = detect_profilarr_layout(config_dir)
    if layout == "v1":
        return os.path.isfile(
            os.path.join(config_dir, "backend", "venv", "bin", "gunicorn")
        ) and os.path.isdir(os.path.join(config_dir, "frontend", "dist"))
    if layout == "v2":
        runtime_dir = os.path.join(config_dir, "runtime")
        return os.path.isfile(os.path.join(runtime_dir, "profilarr")) and all(
            os.path.exists(os.path.join(runtime_dir, name))
            for name in ("server.js", "static")
        )
    return False


def validate_profilarr_legacy_layout(
    instance_name: str, backend_dir: str
) -> tuple[bool, str | None]:
    repo_marker = os.path.join(backend_dir, "app", "main.py")
    if os.path.isfile(repo_marker):
        return True, None
    return (
        False,
        (
            f"Profilarr instance {instance_name} is not using the legacy "
            "backend/frontend layout supported by this integration. Pin "
            f"Profilarr to {PROFILARR_LEGACY_RELEASE_VERSION} or disable "
            "Profilarr until v2 runtime support is added."
        ),
    )


def validate_profilarr_layout(
    instance_name: str, config_dir: str
) -> tuple[str | None, str | None]:
    layout = detect_profilarr_layout(config_dir)
    if layout:
        return layout, None
    return (
        None,
        (
            f"Profilarr instance {instance_name} does not contain a supported "
            "v1 backend/frontend layout or v2 Deno/SvelteKit layout."
        ),
    )


def _normalize_arr_name(instance_key: str, instance: dict, svc_name: str) -> str:
    label = (
        instance.get("instance_name")
        or instance.get("name")
        or instance.get("process_name")
        or instance_key
        or svc_name
    )
    return f"{svc_name}:{label}"


def _build_arr_entries(core_services: list[str], arr_instances=None) -> list[dict]:
    entries = []
    for svc_name in ("sonarr", "radarr"):
        svc_cfg = _setting(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst_key, inst in instances.items():
            if not isinstance(inst, dict) or not inst.get("enabled"):
                continue
            if arr_instances is not None:
                if f"{svc_name}.{inst_key}" not in arr_instances:
                    continue
            else:
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
                logger.warning(
                    "Profilarr auto-link: missing port for %s %s", svc_name, inst_key
                )
                continue

            api_key = _parse_arr_api_key(inst.get("config_file", ""))
            if not api_key:
                logger.warning(
                    "Profilarr auto-link: missing API key for %s %s", svc_name, inst_key
                )
                continue

            entry = {
                "name": _normalize_arr_name(inst_key, inst, svc_name),
                "type": svc_name,
                "tags": [
                    "dumb:auto",
                    f"dumb:instance:{svc_name}.{inst_key}",
                    svc_name.capitalize(),
                    *(
                        [f"core_service:{cs}" for cs in core_services]
                        if core_services
                        else []
                    ),
                ],
                "arr_server": _arr_native_url(inst, port),
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
            "Profilarr repo items not tagged for this app; skipping auto sync seed."
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


def _run_initial_sync(
    backend_dir: str, config_root: str, arr_ids: list[int]
) -> tuple[bool, str | None]:
    if not arr_ids:
        return True, None
    logger.info("Profilarr initial sync starting for %s arr configs", len(arr_ids))
    python_bin = os.path.join(backend_dir, "venv", "bin", "python")
    if not os.path.isfile(python_bin):
        logger.warning("Profilarr venv python not found for initial sync.")
        return False, "Profilarr venv Python was not found"

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
        result = subprocess.run(
            [python_bin, "-c", snippet],
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False, "Profilarr synchronization failed"

        logger.info("Profilarr initial sync completed for %s arr configs", len(arr_ids))
        return True, None
    except Exception:
        return False, "Profilarr synchronization could not be started"


def _arr_native_url(instance: dict, port) -> str:
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


def _eligible_arr_names(core_services, arr_instances=None):
    """Include pending credentials in desired scope so readiness never deletes links."""
    names = set()
    for family in ("sonarr", "radarr"):
        for key, inst in ((_setting(family) or {}).get("instances") or {}).items():
            if not isinstance(inst, dict) or not inst.get("enabled"):
                continue
            if arr_instances is not None:
                if f"{family}.{key}" not in arr_instances:
                    continue
            elif not inst.get("use_profilarr") or (
                core_services
                and not set(get_core_services(inst)).intersection(core_services)
            ):
                continue
            names.add(_normalize_arr_name(key, inst, family))
    return names


def _target_arr_names(target_ids):
    names = set()
    for identifier in target_ids:
        family, _, instance_name = identifier.partition(".")
        instance = ((_setting(family) or {}).get("instances") or {}).get(instance_name)
        if isinstance(instance, dict):
            names.add(_normalize_arr_name(instance_name, instance, family))
    return names


def sync_profilarr_arr_configs(
    profilarr_instance: dict, result_collector=None, *, target_ids=None
) -> tuple[bool, str | None]:
    if not isinstance(profilarr_instance, dict):
        return False, "Profilarr instance config missing"
    identity = str(
        profilarr_instance.get("instance_name")
        or profilarr_instance.get("process_name")
        or "default"
    )

    def report(execution, detail, changed=False, retryable=False):
        emit_action(
            ActionResult(
                action_id=f"profilarr.connections:{identity}",
                service_key="profilarr",
                action="arr_links",
                instance_key=identity,
                execution=execution,
                detail=detail,
                changed=changed,
                retryable=retryable,
            ),
            result_collector,
        )

    core_services = get_core_services(profilarr_instance)
    selected_ids = profilarr_instance.get("arr_instances")
    if selected_ids is not None and (
        not isinstance(selected_ids, list)
        or any(not isinstance(value, str) for value in selected_ids)
    ):
        return (
            False,
            "Profilarr Arr assignments must be a list of canonical instance keys",
        )
    if not core_services and selected_ids is None:
        report(
            "waiting_input",
            "Select a workflow assignment or configure Profilarr connections manually.",
        )
        return True, None
    config_dir = profilarr_instance.get("config_dir", "/profilarr/default")
    config_root = os.path.join(config_dir, "config")
    layout = detect_profilarr_layout(config_dir)
    if layout == "v2":
        link_results = []
        ok, err = _sync_profilarr_v2_arr_configs(
            config_root,
            core_services,
            link_results,
            identity,
            selected_ids,
            target_ids=target_ids,
        )
        for outcome in link_results:
            emit_action(outcome, result_collector)
        incomplete = [
            item
            for item in link_results
            if item.execution in {"waiting_input", "waiting_dependency", "failed"}
        ]
        if ok and incomplete:
            return False, "Profilarr has unfinished Arr connections"
        report(
            "applied" if ok else "failed",
            (
                "Connections reconciled. Select a database, profiles and synchronization policy in Profilarr."
                if ok
                else "Profilarr connection reconciliation failed; check database readiness."
            ),
            retryable=not ok,
        )
        return ok, err
    if layout != "v1":
        report(
            "waiting_dependency",
            "Waiting for a supported Profilarr installation.",
            retryable=True,
        )
        return False, "Profilarr installation layout could not be detected"
    backend_dir = os.path.join(config_dir, "backend")
    repo_path = os.path.join(config_root, "db")
    uid, gid = _setting("puid"), _setting("pgid")
    _ensure_config_ownership(config_root, uid, gid)
    _ensure_repo_ownership(repo_path, uid, gid)
    ok, err = _ensure_profilarr_db(backend_dir, config_root)
    if not ok:
        report("failed", "Profilarr database initialization failed.", retryable=True)
        return False, "Profilarr DB migration failed"
    ok, err = _ensure_default_repo(backend_dir, config_root)
    if not ok:
        report(
            "failed",
            "Profilarr repository initialization failed. Retry or select a repository in Profilarr.",
            retryable=True,
        )
        return False, "Profilarr default repository initialization failed"
    _ensure_config_ownership(config_root, uid, gid)
    _ensure_repo_ownership(repo_path, uid, gid)
    db_path = os.path.join(config_root, "profilarr.db")
    if not os.path.isfile(db_path):
        report(
            "waiting_dependency",
            "Waiting for the initialized Profilarr database.",
            retryable=True,
        )
        return False, "Profilarr DB not found after migration"
    entries = (
        _build_arr_entries(core_services, selected_ids)
        if selected_ids is not None
        else _build_arr_entries(core_services)
    )
    desired_names = {entry["name"] for entry in entries} | _eligible_arr_names(
        core_services, selected_ids
    )
    scoped_names = _target_arr_names(target_ids) if target_ids is not None else None
    conflicts = []
    changed = False
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            existing = conn.execute("SELECT * FROM arr_config").fetchall()
            by_name = {row["name"]: row for row in existing}
            managed = {
                row["name"]
                for row in existing
                if "dumb:auto" in _json_tags(row["tags"])
            }
            for entry in entries:
                if scoped_names is not None and entry["name"] not in scoped_names:
                    continue
                row = by_name.get(entry["name"])
                if row:
                    if entry["name"] not in managed:
                        conflicts.append(entry["name"])
                        continue
                    tags = list(entry["tags"])
                    tags.extend(
                        tag
                        for tag in _json_tags(row["tags"])
                        if tag not in tags and not tag.startswith("core_service:")
                    )
                    # Connection-only updates preserve profile selections, user tags,
                    # intervals and sync policy. No implicit initial import/search.
                    wanted = (
                        entry["type"],
                        json.dumps(tags),
                        entry["arr_server"],
                        entry["api_key"],
                    )
                    previous = (
                        row["type"],
                        row["tags"],
                        row["arr_server"],
                        row["api_key"],
                    )
                    if wanted != previous:
                        conn.execute(
                            "UPDATE arr_config SET type=?, tags=?, arr_server=?, api_key=? WHERE id=?",
                            (*wanted, row["id"]),
                        )
                        changed = True
                else:
                    conn.execute(
                        """INSERT INTO arr_config
                        (name, type, tags, arr_server, api_key, data_to_sync,
                         sync_method, sync_interval, import_as_unique)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            entry["name"],
                            entry["type"],
                            json.dumps(entry["tags"]),
                            entry["arr_server"],
                            entry["api_key"],
                            json.dumps({"profiles": [], "customFormats": []}),
                            "manual",
                            0,
                            0,
                        ),
                    )
                    changed = True
            stale = managed - desired_names
            if scoped_names is not None:
                stale &= scoped_names
            if stale:
                conn.execute(
                    "DELETE FROM arr_config WHERE name IN ({})".format(
                        ",".join("?" for _ in stale)
                    ),
                    tuple(stale),
                )
                changed = True
            conn.commit()
    except sqlite3.Error:
        report(
            "failed",
            "Profilarr database update failed; transaction changes were rolled back.",
            retryable=True,
        )
        return False, "Profilarr database update failed"
    if conflicts:
        report(
            "waiting_input",
            "A user-managed Arr connection matches a managed name. Configure it manually or explicitly rename it.",
            changed=changed,
        )
        return False, "Profilarr user-managed connection requires manual review"
    pending = desired_names - {entry["name"] for entry in entries}
    if scoped_names is not None:
        pending &= scoped_names
    if pending:
        report(
            "waiting_dependency",
            "Waiting for selected Arr ports or API keys. Existing connections were retained.",
            changed=changed,
            retryable=True,
        )
        return False, "Profilarr is waiting for selected Arr credentials"
    report(
        "applied" if changed else "already_present",
        "Connections reconciled. Select profiles and synchronization policy in Profilarr.",
        changed=changed,
    )
    return True, None


def _json_tags(value) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(tag) for tag in parsed] if isinstance(parsed, list) else []


def _sync_profilarr_v2_arr_configs(
    config_root: str,
    core_services: list[str],
    result_collector=None,
    instance_key="default",
    arr_instances=None,
    *,
    target_ids=None,
) -> tuple[bool, str | None]:
    """Reconcile DUMB-managed Arr connections in Profilarr v2's application DB.

    Profilarr v2 owns database/profile selection and sync policy. DUMB only manages
    the Arr connection rows marked with ``dumb:auto``; it does not choose profiles
    or trigger an initial sync on the operator's behalf.
    """
    db_path = os.path.join(config_root, "data", "profilarr.db")
    if not os.path.isfile(db_path):
        return False, "Profilarr v2 database is not initialized yet"

    entries = (
        _build_arr_entries(core_services, arr_instances)
        if arr_instances is not None
        else _build_arr_entries(core_services)
    )
    pending_results = []
    scoped_names = _target_arr_names(target_ids) if target_ids is not None else None

    def link_result(entry, execution, detail, changed=False, retryable=False):
        target = next(
            (
                tag.removeprefix("dumb:instance:")
                for tag in entry.get("tags", [])
                if tag.startswith("dumb:instance:")
            ),
            entry["name"],
        )
        pending_results.append(
            ActionResult(
                action_id=f"profilarr.link:{instance_key}->{target}",
                service_key="profilarr",
                action="arr_link",
                instance_key=instance_key,
                execution=execution,
                detail=detail,
                changed=changed,
                retryable=retryable,
            )
        )

    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(arr_instances)").fetchall()
            }
            required = {"id", "name", "type", "url", "api_key", "tags", "enabled"}
            if not required.issubset(columns):
                return False, "Profilarr v2 database migrations are not ready yet"

            existing = conn.execute(
                "SELECT id, name, tags, enabled, type, url, api_key FROM arr_instances"
            ).fetchall()
            existing_by_name = {row["name"]: row for row in existing}
            managed = {
                row["name"]
                for row in existing
                if "dumb:auto" in _json_tags(row["tags"])
            }
            desired_names = {entry["name"] for entry in entries} | _eligible_arr_names(
                core_services, arr_instances
            )

            for entry in entries:
                if scoped_names is not None and entry["name"] not in scoped_names:
                    continue
                row = existing_by_name.get(entry["name"])
                tags = list(entry["tags"])
                if row:
                    if entry["name"] not in managed:
                        logger.warning(
                            "Profilarr v2 auto-link left user-managed Arr instance "
                            "unchanged because its name matches a DUMB service."
                        )
                        link_result(
                            entry,
                            "waiting_input",
                            "A matching user-managed Profilarr connection requires manual review.",
                        )
                        continue
                    if not row["enabled"]:
                        link_result(
                            entry,
                            "waiting_input",
                            "The managed Profilarr connection is disabled. Enable it when ready.",
                        )
                    preserved_tags = [
                        tag
                        for tag in _json_tags(row["tags"])
                        if tag not in {"dumb:auto", "Sonarr", "Radarr"}
                        and not tag.startswith("core_service:")
                    ]
                    tags.extend(tag for tag in preserved_tags if tag not in tags)
                    changed = (
                        row["type"] != entry["type"]
                        or row["url"] != entry["arr_server"]
                        or row["api_key"] != entry["api_key"]
                        or set(_json_tags(row["tags"])) != set(tags)
                    )
                    logger.debug("Profilarr v2 auto-link updating Arr instance.")
                    if row["enabled"]:
                        link_result(
                            entry,
                            "applied" if changed else "already_present",
                            "Profilarr connection configured; select profiles and sync policy separately.",
                            changed=changed,
                        )
                    conn.execute(
                        """
                        UPDATE arr_instances
                        SET type = ?, url = ?, api_key = ?, tags = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (
                            entry["type"],
                            entry["arr_server"],
                            entry["api_key"],
                            json.dumps(tags),
                            row["id"],
                        ),
                    )
                else:
                    logger.info("Profilarr v2 auto-link creating Arr instance.")
                    link_result(
                        entry,
                        "applied",
                        "Profilarr connection created with no profile synchronization enabled.",
                        changed=True,
                    )
                    conn.execute(
                        """
                        INSERT INTO arr_instances
                            (name, type, url, api_key, tags, enabled)
                        VALUES (?, ?, ?, ?, ?, 1)
                        """,
                        (
                            entry["name"],
                            entry["type"],
                            entry["arr_server"],
                            entry["api_key"],
                            json.dumps(tags),
                        ),
                    )

            stale = managed - desired_names
            if scoped_names is not None:
                stale &= scoped_names
            if stale:
                logger.info(
                    "Profilarr v2 auto-link removing %s stale Arr instances", len(stale)
                )
                conn.execute(
                    "DELETE FROM arr_instances WHERE name IN ({})".format(
                        ",".join("?" for _ in stale)
                    ),
                    tuple(stale),
                )
            conn.commit()
    except sqlite3.Error as exc:
        return False, f"Profilarr v2 database update failed: {exc}"

    for name in _eligible_arr_names(core_services, arr_instances) - {
        entry["name"] for entry in entries
    }:
        if scoped_names is not None and name not in scoped_names:
            continue
        link_result(
            {"name": name},
            "waiting_dependency",
            "Waiting for selected Arr credentials; existing connection retained.",
            retryable=True,
        )
    for outcome in pending_results:
        emit_action(outcome, result_collector)
    logger.info("Profilarr v2 auto-link reconciled %s Arr instances.", len(entries))
    return True, None


def _ensure_repo_ownership(
    repo_path: str, user_id: int | None, group_id: int | None
) -> None:
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


def _ensure_config_ownership(
    config_root: str, user_id: int | None, group_id: int | None
) -> None:
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
        svc_cfg = _setting(svc_name) or {}
        instances = (svc_cfg.get("instances") or {}) or {}
        for inst in instances.values():
            if not isinstance(inst, dict) or not inst.get("enabled"):
                continue
            if inst.get("use_profilarr"):
                return True
    return False


def _patch_profilarr_config(
    result_collector=None, target_ids=None
) -> tuple[bool, str | None]:
    profilarr_cfg = _setting("profilarr") or {}
    instances = (profilarr_cfg.get("instances") or {}) or {}
    if not isinstance(instances, dict) or not instances:
        return True, None

    errors = []
    for inst in instances.values():
        if not isinstance(inst, dict) or not inst.get("enabled"):
            continue
        ok, err = sync_profilarr_arr_configs(
            inst, result_collector=result_collector, target_ids=target_ids
        )
        if not ok:
            errors.append(err or "Unknown error")
    if errors:
        return False, "; ".join(errors)
    return True, None


def patch_profilarr_config(
    result_collector=None, *, configuration=None, target_ids=None
) -> tuple[bool, str | None]:
    token = _CONFIG_OVERRIDE.set(configuration)
    try:
        return _patch_profilarr_config(result_collector, target_ids)
    finally:
        _CONFIG_OVERRIDE.reset(token)
