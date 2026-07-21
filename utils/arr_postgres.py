import os
import re
import xml.etree.ElementTree as StdET

import defusedxml.ElementTree as ET

from utils.global_logger import logger

ARR_POSTGRES_KEYS = ("sonarr", "radarr", "lidarr", "prowlarr", "whisparr")
ARR_POSTGRES_XML_TAGS = (
    "PostgresUser",
    "PostgresPassword",
    "PostgresHost",
    "PostgresPort",
    "PostgresMainDb",
    "PostgresLogDb",
)


def arr_postgres_enabled(instance: dict) -> bool:
    """Only explicit true opts an Arr instance into PostgreSQL."""
    return isinstance(instance, dict) and instance.get("postgres_enabled") is True


def read_arr_postgres_xml_values(instance: dict) -> dict[str, str]:
    if not isinstance(instance, dict):
        return {}
    config_file = instance.get("config_file")
    if not config_file or not os.path.exists(config_file):
        return {}
    try:
        root = ET.parse(config_file).getroot()
    except Exception as exc:
        logger.debug(
            "Unable to inspect Arr PostgreSQL XML config %s: %s", config_file, exc
        )
        return {}

    values = {}
    for tag in ARR_POSTGRES_XML_TAGS:
        elem = root.find(tag)
        if elem is not None and elem.text:
            values[tag] = elem.text.strip()
    return values


def arr_postgres_config_file_enabled(instance: dict) -> bool:
    values = read_arr_postgres_xml_values(instance)
    return bool(
        values.get("PostgresHost")
        and values.get("PostgresMainDb")
        and values.get("PostgresLogDb")
    )


def ensure_arr_postgres_enabled_flag(instance_name: str, instance: dict) -> bool:
    if arr_postgres_enabled(instance):
        return False
    if not arr_postgres_config_file_enabled(instance):
        return False
    instance["postgres_enabled"] = True
    return True


def _slugify_instance_name(instance_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(instance_name or "").lower()).strip("_")
    return slug or "default"


def arr_postgres_database_names(
    key: str, instance_name: str, instance: dict
) -> tuple[str, str]:
    main_db = (instance.get("postgres_main_db") or "").strip()
    log_db = (instance.get("postgres_log_db") or "").strip()
    if main_db and log_db:
        return main_db, log_db

    xml_values = read_arr_postgres_xml_values(instance)
    xml_main_db = (xml_values.get("PostgresMainDb") or "").strip()
    xml_log_db = (xml_values.get("PostgresLogDb") or "").strip()

    if str(instance_name or "").lower() == "default":
        default_main = f"{key}-main"
        default_log = f"{key}-log"
    else:
        slug = _slugify_instance_name(instance_name)
        default_main = f"{key}_{slug}_main"
        default_log = f"{key}_{slug}_log"

    return main_db or xml_main_db or default_main, log_db or xml_log_db or default_log


def iter_postgres_arr_instances(config_manager):
    for key in ARR_POSTGRES_KEYS:
        instances = config_manager.get(key, {}).get("instances", {}) or {}
        for instance_name, instance in instances.items():
            if not isinstance(instance, dict):
                continue
            if instance.get("enabled") and arr_postgres_enabled(instance):
                yield key, instance_name, instance


def configure_arr_postgres_runtime(config_manager) -> bool:
    """
    Enable PostgreSQL and register Arr main/log databases for opted-in instances.

    Returns True when the runtime config changed.
    """
    arr_databases: list[str] = []
    changed = False
    for key in ARR_POSTGRES_KEYS:
        instances = config_manager.get(key, {}).get("instances", {}) or {}
        for instance_name, instance in instances.items():
            if not isinstance(instance, dict) or not instance.get("enabled"):
                continue
            if ensure_arr_postgres_enabled_flag(instance_name, instance):
                changed = True
                logger.info(
                    "Persisted postgres_enabled=true for Arr PostgreSQL instance: %s",
                    instance.get("process_name") or instance_name,
                )
            if not arr_postgres_enabled(instance):
                continue
            main_db, log_db = arr_postgres_database_names(key, instance_name, instance)
            arr_databases.extend([main_db, log_db])

    if not arr_databases:
        return changed

    postgres_config = config_manager.get("postgres", {}) or {}
    if not postgres_config.get("enabled"):
        postgres_config["enabled"] = True
        changed = True
        logger.info("PostgreSQL enabled because Arr PostgreSQL support is configured.")

    databases = postgres_config.setdefault("databases", [])
    for db_name in arr_databases:
        existing = next(
            (
                db
                for db in databases
                if isinstance(db, dict) and str(db.get("name")) == db_name
            ),
            None,
        )
        if existing is not None:
            if existing.get("enabled") is not True:
                existing["enabled"] = True
                changed = True
        else:
            databases.append({"name": db_name, "enabled": True})
            changed = True
            logger.info("Registered PostgreSQL database for Arr service: %s", db_name)

    return changed


def _set_xml_text(root, tag: str, value) -> bool:
    value_text = "" if value is None else str(value)
    elem = root.find(tag)
    if elem is None:
        elem = StdET.SubElement(root, tag)
        elem.text = value_text
        return True
    if (elem.text or "") != value_text:
        elem.text = value_text
        return True
    return False


def apply_arr_postgres_config(
    key: str,
    instance_name: str,
    instance: dict,
    config_file: str,
    postgres_config: dict,
) -> bool:
    if not arr_postgres_enabled(instance):
        return False

    if not os.path.exists(config_file):
        logger.warning(
            "[%s] Cannot apply PostgreSQL settings before config.xml exists: %s",
            instance.get("process_name") or key.capitalize(),
            config_file,
        )
        return False

    main_db, log_db = arr_postgres_database_names(key, instance_name, instance)
    values = {
        "PostgresUser": postgres_config.get("user", "DUMB"),
        "PostgresPassword": postgres_config.get("password", "postgres"),
        "PostgresPort": postgres_config.get("port", 5432),
        "PostgresHost": postgres_config.get("host", "127.0.0.1"),
        "PostgresMainDb": main_db,
        "PostgresLogDb": log_db,
    }

    tree = ET.parse(config_file)
    root = tree.getroot()
    changed = False
    for tag, value in values.items():
        changed = _set_xml_text(root, tag, value) or changed

    if changed:
        tree.write(config_file, encoding="utf-8", xml_declaration=True)
        logger.info(
            "[%s] Updated config.xml for PostgreSQL databases %s and %s.",
            instance.get("process_name") or key.capitalize(),
            main_db,
            log_db,
        )
    return changed
