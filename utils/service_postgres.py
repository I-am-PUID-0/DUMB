"""PostgreSQL configuration helpers for non-Arr dual-backend services."""

from __future__ import annotations

import os
import re
from urllib.parse import quote

import yaml

from utils.global_logger import logger

SERVICE_POSTGRES_KEYS = ("altmount", "bazarr", "pulsarr", "seerr")


def service_postgres_enabled(service: dict | None) -> bool:
    return isinstance(service, dict) and service.get("postgres_enabled") is True


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").lower()).strip("_") or "default"


def service_postgres_database_name(
    key: str, instance_name: str | None, service: dict
) -> str:
    configured = str(service.get("postgres_database") or "").strip()
    if configured:
        return configured
    if instance_name and str(instance_name).lower() != "default":
        return f"{key}_{_slug(instance_name)}"
    return key


def postgres_connection_values(postgres_config: dict, database: str) -> dict[str, str]:
    return {
        "host": str(postgres_config.get("host") or "127.0.0.1"),
        "port": str(postgres_config.get("port") or 5432),
        "user": str(postgres_config.get("user") or "DUMB"),
        "password": str(postgres_config.get("password") or "postgres"),
        "database": database,
    }


def postgres_dsn(postgres_config: dict, database: str) -> str:
    values = postgres_connection_values(postgres_config, database)
    host = values["host"]
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return "postgres://{}:{}@{}:{}/{}?sslmode=disable".format(
        quote(values["user"], safe=""),
        quote(values["password"], safe=""),
        host,
        values["port"],
        quote(values["database"], safe=""),
    )


def apply_service_postgres_config(
    key: str,
    service: dict,
    postgres_config: dict,
    database: str,
    *,
    enabled: bool,
) -> bool:
    """Apply a service's runtime database selection without starting it."""
    if key not in SERVICE_POSTGRES_KEYS:
        raise ValueError(f"Unsupported PostgreSQL service: {key}")

    values = postgres_connection_values(postgres_config, database)
    changed = False

    if key == "altmount":
        config_dir = str(service.get("config_dir") or "/altmount")
        config_file = str(
            service.get("config_file") or os.path.join(config_dir, "config.yaml")
        )
        # AltMount's setup owns creation of the complete first-run document.
        # Writing a database-only file here would make that initializer skip.
        if not os.path.isfile(config_file):
            return False
        data = {}
        try:
            with open(config_file, "r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
                data = loaded if isinstance(loaded, dict) else {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Could not update AltMount database config: %s", exc)
            return False
        database_config = data.setdefault("database", {})
        desired = (
            {
                "type": "postgres",
                "path": os.path.join(config_dir, "altmount.db"),
                "dsn": postgres_dsn(postgres_config, database),
            }
            if enabled
            else {
                "type": "sqlite",
                "path": os.path.join(config_dir, "altmount.db"),
            }
        )
        for name, value in desired.items():
            if database_config.get(name) != value:
                database_config[name] = value
                changed = True
        if not enabled and database_config.pop("dsn", None) is not None:
            changed = True
        if changed:
            temporary = f"{config_file}.tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                yaml.safe_dump(data, handle, sort_keys=False)
            try:
                stat = os.stat(config_file)
                os.chmod(temporary, stat.st_mode & 0o777)
                os.chown(temporary, stat.st_uid, stat.st_gid)
            except OSError:
                pass
            os.replace(temporary, config_file)
        return changed

    env = service.setdefault("env", {})
    if key == "bazarr":
        desired = {"POSTGRES_ENABLED": "true" if enabled else "false"}
        connection_fields = {
            "POSTGRES_HOST": values["host"],
            "POSTGRES_PORT": values["port"],
            "POSTGRES_DATABASE": values["database"],
            "POSTGRES_USERNAME": values["user"],
            "POSTGRES_PASSWORD": values["password"],
        }
    elif key == "pulsarr":
        desired = {"dbType": "postgres" if enabled else "sqlite"}
        connection_fields = {
            "dbHost": values["host"],
            "dbPort": values["port"],
            "dbName": values["database"],
            "dbUser": values["user"],
            "dbPassword": values["password"],
        }
    else:
        desired = {"DB_TYPE": "postgres" if enabled else "sqlite"}
        connection_fields = {
            "DB_HOST": values["host"],
            "DB_PORT": values["port"],
            "DB_NAME": values["database"],
            "DB_USER": values["user"],
            "DB_PASS": values["password"],
        }
    if enabled:
        desired.update(connection_fields)
    else:
        for name in connection_fields:
            if env.pop(name, None) is not None:
                changed = True
    for name, value in desired.items():
        if env.get(name) != value:
            env[name] = value
            changed = True
    return changed


def iter_postgres_services(config_manager):
    for key in SERVICE_POSTGRES_KEYS:
        section = config_manager.get(key, {}) or {}
        if isinstance(section.get("instances"), dict):
            for instance_name, service in section["instances"].items():
                if (
                    isinstance(service, dict)
                    and service.get("enabled")
                    and service_postgres_enabled(service)
                ):
                    yield key, instance_name, service
        elif section.get("enabled") and service_postgres_enabled(section):
            yield key, None, section


def configure_service_postgres_runtime(config_manager) -> bool:
    """Register databases and synchronize runtime config for opted-in services."""
    selected = list(iter_postgres_services(config_manager))
    if not selected:
        return False
    postgres_config = config_manager.get("postgres", {}) or {}
    changed = False
    if not postgres_config.get("enabled"):
        postgres_config["enabled"] = True
        changed = True
    databases = postgres_config.setdefault("databases", [])
    for key, instance_name, service in selected:
        database = service_postgres_database_name(key, instance_name, service)
        entry = next(
            (
                item
                for item in databases
                if isinstance(item, dict) and str(item.get("name")) == database
            ),
            None,
        )
        if entry is not None:
            if entry.get("enabled") is not True:
                entry["enabled"] = True
                changed = True
        else:
            databases.append({"name": database, "enabled": True})
            changed = True
        if apply_service_postgres_config(
            key, service, postgres_config, database, enabled=True
        ):
            changed = True
            logger.info(
                "Synchronized DUMB-managed PostgreSQL configuration for %s.",
                service.get("process_name") or key,
            )
    return changed
