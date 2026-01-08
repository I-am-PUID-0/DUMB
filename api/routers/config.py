from fastapi import APIRouter, HTTPException, Depends, Query, Request
from typing import Union, Optional, Dict, Any, List
from pydantic import BaseModel
from utils.dependencies import get_logger, get_process_handler, resolve_path
from utils.config_loader import CONFIG_MANAGER, find_service_config
from utils.traefik_setup import (
    ensure_ui_services_config,
    get_traefik_config_dir,
    setup_traefik,
)
from jsonschema import validate, ValidationError
from ruamel.yaml import YAML
import os, json, configparser, xmltodict, ast


class ConfigUpdateRequest(BaseModel):
    process_name: Optional[str] = None
    updates: Dict[str, Any]
    persist: bool = False


class ServiceConfigRequest(BaseModel):
    service_name: str
    updates: Union[dict, str, list] = None


class ProcessSchemaRequest(BaseModel):
    process_name: str


config_router = APIRouter()


class ServiceUiToggleRequest(BaseModel):
    enabled: bool = True


def validate_file_path(file_path):
    if not file_path.exists():
        raise HTTPException(
            status_code=500, detail=f"File path {file_path} does not exist."
        )
    if not os.access(file_path, os.W_OK):
        raise HTTPException(
            status_code=500, detail=f"Cannot write to file path {file_path}."
        )


def write_to_file(file_path, content):
    try:
        with open(file_path, "w") as file:
            if isinstance(content, str):
                file.write(content)
            elif isinstance(content, list):
                file.writelines(content)
            file.flush()
            os.fsync(file.fileno())
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to write to file {file_path}: {e}"
        )


def _normalize_direct_url(service, request: Request):
    direct_url = service.get("direct_url")
    if not direct_url:
        return service
    if service.get("direct_url_locked"):
        return service
    host = (service.get("host") or "").lower()
    if host and host not in ("127.0.0.1", "0.0.0.0", "::", "localhost"):
        return service
    port = service.get("port")
    if not port:
        return service
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    forwarded_host = request.headers.get("x-forwarded-host")
    host_header = forwarded_host or request.headers.get("host")
    request_host = None
    if host_header:
        request_host = host_header.split(",")[0].split(":")[0].strip()
    if not request_host:
        return service
    service["direct_url"] = f"{scheme}://{request_host}:{port}/"
    return service


def find_service_config(config, service_name, parent_path=""):
    for key, value in config.items():

        if isinstance(value, dict) and value.get("process_name") == service_name:
            return value, (f"{parent_path}.{key}" if parent_path else key)

        if isinstance(value, dict) and "instances" in value:
            for instance_name, instance in value["instances"].items():
                if (
                    isinstance(instance, dict)
                    and instance.get("process_name") == service_name
                ):
                    return instance, (
                        f"{parent_path}.{key}.instances.{instance_name}"
                        if parent_path
                        else f"{key}.instances.{instance_name}"
                    )

        if isinstance(value, dict):
            found, path = find_service_config(
                value, service_name, f"{parent_path}.{key}" if parent_path else key
            )
            if found:
                return found, path

    return None, None


def load_config_file(config_path):
    yaml = YAML(typ="rt")
    raw_config = None
    config_data = None
    config_format = None

    try:
        if config_path.suffix == ".json":
            with config_path.open("r") as file:
                raw_config = file.read()
                config_data = json.loads(raw_config)
                config_format = "json"
        elif config_path.suffix in [".yaml", ".yml"]:
            with config_path.open("r") as file:
                raw_config = file.read()
                config_data = yaml.load(raw_config)
                config_format = "yaml"
        elif config_path.suffix in [".conf", ".config"]:
            if "postgresql" in config_path.name.lower():
                lines, config_data = parse_postgresql_conf(config_path)
                raw_config = "".join(lines)
                config_format = "postgresql"
            else:
                config_data, raw_config = parse_rclone_config(config_path)
                config_format = "rclone"
        elif config_path.suffix == ".ini":
            config_data, raw_config = parse_ini_config(config_path)
            config_format = "ini"
        elif config_path.suffix == ".py":
            config_data = parse_python_config(config_path)
            with open(config_path, "r") as file:
                raw_config = file.read()
                config_format = "python"
        elif config_path.suffix == ".xml":
            with config_path.open("r", encoding="utf-8") as file:
                raw_config = file.read()
                config_data = xmltodict.parse(raw_config)
                config_format = "xml"
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported config file format: {config_path.suffix}",
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load config file: {e}")

    return raw_config, config_data, config_format


def save_config_file(config_path, config_data, config_format, updates=None):
    yaml = YAML(typ="rt")
    try:
        if updates:
            if isinstance(updates, dict):
                if config_format == "xml":
                    if len(config_data) == 1:
                        root_key = next(iter(config_data))
                        root_val = config_data.get(root_key)
                        if root_key not in updates and isinstance(root_val, dict):
                            root_val.update(updates)
                        else:
                            config_data.update(updates)
                    else:
                        config_data.update(updates)
                else:
                    config_data.update(updates)
            elif isinstance(updates, str):
                if config_format == "yaml":
                    updates_dict = yaml.load(updates)
                    config_data.update(updates_dict)
                elif config_format == "json":
                    updates_dict = json.loads(updates)
                    config_data.update(updates_dict)
                elif config_format == "postgresql":
                    write_postgresql_conf(config_path, updates)
                    return
                elif config_format == "rclone":
                    write_rclone_config(config_path, updates)
                    return
                elif config_format == "python":
                    write_python_config(config_path, updates)
                    return
                elif config_format == "xml":
                    write_to_file(config_path, updates)
                    return
                elif config_format == "ini":
                    write_ini_config(config_path, updates)
                    return
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Unsupported updates format for {config_format}.",
                    )

        if config_format == "json":
            write_to_file(config_path, json.dumps(config_data, indent=4))
        elif config_format == "yaml":
            yaml.indent(mapping=2, sequence=4, offset=2)
            yaml.preserve_quotes = True
            with open(config_path, "w") as file:
                yaml.dump(config_data, file)
        elif config_format == "postgresql":
            write_postgresql_conf(config_path, config_data)
        elif config_format == "rclone":
            write_rclone_config(config_path, config_data)
        elif config_format == "python":
            write_python_config(config_path, config_data)
        elif config_format == "xml":
            existing_xml = ""
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as file:
                    existing_xml = file.read()
            existing_has_decl = existing_xml.lstrip().startswith("<?xml")
            indent_str = None
            for line in existing_xml.splitlines():
                stripped = line.lstrip()
                if not stripped or stripped.startswith("<?xml"):
                    continue
                if line.startswith(("<", "</")):
                    continue
                if stripped.startswith("<"):
                    indent_str = line[: len(line) - len(stripped)]
                    break
            xml_text = xmltodict.unparse(config_data, pretty=True)
            if isinstance(xml_text, bytes):
                xml_text = xml_text.decode("utf-8")
            if indent_str is not None:
                xml_lines = []
                for line in xml_text.splitlines():
                    tab_count = 0
                    for ch in line:
                        if ch == "\t":
                            tab_count += 1
                        else:
                            break
                    if tab_count:
                        line = f"{indent_str * tab_count}{line[tab_count:]}"
                    xml_lines.append(line)
                xml_text = "\n".join(xml_lines)
            if existing_has_decl and not xml_text.lstrip().startswith("<?xml"):
                xml_text = f'<?xml version="1.0" encoding="utf-8"?>\n{xml_text}'
            if not existing_has_decl and xml_text.lstrip().startswith("<?xml"):
                xml_text = xml_text.lstrip()
                xml_text = "\n".join(xml_text.splitlines()[1:])
            write_to_file(config_path, xml_text)
        elif config_format == "ini":
            write_ini_config(config_path, config_data)
        else:
            raise HTTPException(
                status_code=400, detail=f"Unsupported config format: {config_format}"
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save config file: {e}")


def parse_postgresql_conf(file_path):
    config = {}
    lines = []
    with open(file_path, "r") as file:
        for line in file:
            stripped = line.strip()
            lines.append(line)

            if not stripped or stripped.startswith("#"):
                continue

            if "=" in stripped:
                key, value = map(str.strip, stripped.split("=", 1))
                config[key] = value
            elif " " in stripped:
                key, value = map(str.strip, stripped.split(None, 1))
                config[key] = value
    return lines, config


def write_postgresql_conf(file_path, updates):
    validate_file_path(file_path)

    try:
        with open(file_path, "r") as file:
            lines = file.readlines()

        if isinstance(updates, str):
            write_to_file(file_path, updates)
            return

        elif isinstance(updates, dict):
            for key, value in updates.items():
                if isinstance(value, bool):
                    formatted_value = "on" if value else "off"
                elif isinstance(value, (int, float)):
                    formatted_value = str(value)
                elif isinstance(value, str):
                    if not (
                        value[-2:]
                        in ["MB", "GB", "kB", "TB", "ms", "s", "min", "h", "d"]
                        or value[-1:] == "B"
                    ):
                        formatted_value = f"'{value}'"
                    else:
                        formatted_value = value
                else:
                    raise ValueError(
                        f"Unsupported value type: {type(value)} for key {key}"
                    )

                for i, line in enumerate(lines):
                    if line.strip().startswith(f"{key} ="):
                        lines[i] = f"{key} = {formatted_value}\n"
                        break
                else:
                    lines.append(f"{key} = {formatted_value}\n")

        write_to_file(file_path, lines)

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to write PostgreSQL config: {e}"
        )


def parse_rclone_config(file_path):
    parser = configparser.ConfigParser()
    parser.read(file_path)
    config_data = {
        section: dict(parser.items(section)) for section in parser.sections()
    }

    with open(file_path, "r") as file:
        raw_text = file.read()

    return config_data, raw_text


def write_rclone_config(file_path, config_data):
    validate_file_path(file_path)

    if isinstance(config_data, str):
        write_to_file(file_path, config_data)
        return
    if not isinstance(config_data, dict):
        raise ValueError("Expected raw string or dict for Rclone config.")

    parser = configparser.ConfigParser()
    parser.optionxform = str
    for section, values in config_data.items():
        parser[section] = {}
        if isinstance(values, dict):
            for key, value in values.items():
                parser[section][key] = str(value)

    with open(file_path, "w") as file:
        parser.write(file)


def parse_ini_config(file_path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(file_path)
    config_data = {
        section: dict(parser.items(section, raw=True)) for section in parser.sections()
    }

    with open(file_path, "r") as file:
        raw_text = file.read()

    return config_data, raw_text


def write_ini_config(file_path, config_data):
    validate_file_path(file_path)

    if isinstance(config_data, str):
        write_to_file(file_path, config_data)
        return
    if not isinstance(config_data, dict):
        raise ValueError("Expected raw string or dict for INI config.")

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    for section, values in config_data.items():
        parser[section] = {}
        if isinstance(values, dict):
            for key, value in values.items():
                parser[section][key] = str(value)

    with open(file_path, "w") as file:
        parser.write(file)


def parse_python_config(file_path):
    exec_env = {}
    with open(file_path, "r") as file:
        exec(file.read(), {}, exec_env)
    return {k: v for k, v in exec_env.items() if not k.startswith("__")}


def write_python_config(file_path, config_data):
    validate_file_path(file_path)

    if isinstance(config_data, str):
        config_data = ast.literal_eval(config_data)

    with open(file_path, "w") as file:
        for key, value in config_data.items():
            if isinstance(value, str):
                file.write(f'{key} = "{value}"\n')
            elif isinstance(value, dict):
                file.write(f"{key} = {value}\n")
            else:
                file.write(f"{key} = {value}\n")


def find_schema(schema, path_parts):
    schema_section = schema

    for idx, part in enumerate(path_parts):

        if isinstance(schema_section, dict) and part in schema_section:
            schema_section = schema_section[part]
            continue

        if "properties" in schema_section and part in schema_section["properties"]:
            schema_section = schema_section["properties"][part]
            continue

        if "patternProperties" in schema_section:
            for pattern, sub_schema in schema_section["patternProperties"].items():
                if pattern == ".*" or pattern in part:
                    schema_section = sub_schema
                    break
            else:
                return None

        else:
            return None

    return schema_section


@config_router.get("/")
async def get_config(
    process_name: Optional[str] = Query(
        None, description="If set, return only that service’s config"
    ),
    logger=Depends(get_logger),
):
    if process_name:
        service_cfg, _ = find_service_config(CONFIG_MANAGER.config, process_name)
        if not service_cfg:
            logger.error(f"Service not found: {process_name}")
            raise HTTPException(status_code=404, detail="Service not found")
        return service_cfg

    return CONFIG_MANAGER.config


@config_router.post("/")
async def update_config(
    request: ConfigUpdateRequest,
    logger=Depends(get_logger),
):
    if request.process_name:
        process_name = request.process_name
        updates = request.updates
        persist = request.persist

        logger.info(
            f"Received update request for service '{process_name}' (persist={persist})"
        )

        service_config, service_path = find_service_config(
            CONFIG_MANAGER.config, process_name
        )
        if not service_config:
            logger.error(f"Service not found: {process_name}")
            raise HTTPException(status_code=404, detail="Service not found.")

        path_parts = service_path.split(".")
        instance_schema = find_schema(
            CONFIG_MANAGER.schema.get("properties", {}), path_parts
        )
        if not instance_schema:
            logger.error(f"Schema not found for service: {process_name}")
            raise HTTPException(
                status_code=400,
                detail=f"Schema not found for service: {process_name}",
            )
        try:
            validate(instance=updates, schema=instance_schema)
        except ValidationError as e:
            loc = " -> ".join(map(str, e.absolute_path)) or "root"
            raise HTTPException(
                status_code=400,
                detail=f"Validation error in updates at '{loc}': {e.message}",
            )

        try:
            merged = {**service_config, **updates}
            validate(instance=merged, schema=instance_schema)
        except ValidationError as e:
            loc = " -> ".join(map(str, e.absolute_path)) or "root"
            raise HTTPException(
                status_code=400,
                detail=f"Validation error in merged config at '{loc}': {e.message}",
            )

        for key, value in updates.items():
            if key in service_config:
                service_config[key] = value
            else:
                logger.error(f"Invalid configuration key for {process_name}: {key}")
                raise HTTPException(
                    status_code=400, detail=f"Invalid configuration key: {key}"
                )

        if persist:
            logger.info(f"Persisting updated config for service '{process_name}'")
            CONFIG_MANAGER.save_config(process_name=process_name)

        return {
            "status": "service config updated",
            "process_name": process_name,
            "persisted": persist,
        }

    updates = request.updates
    if not updates:
        raise HTTPException(
            status_code=400, detail="No updates provided for global config."
        )

    logger.info("Performing global config update (deep merge)")

    for key, value in updates.items():
        existing = CONFIG_MANAGER.config.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            existing.update(value)
        else:
            CONFIG_MANAGER.config[key] = value

    CONFIG_MANAGER.save_config()

    return {"status": "global config updated", "keys": list(updates.keys())}


@config_router.get("/schema")
def get_config_schema():
    return CONFIG_MANAGER.schema


@config_router.post("/process-config/schema")
def get_service_config_schema(req: ProcessSchemaRequest) -> Dict[str, Any]:
    if not (CONFIG_MANAGER and CONFIG_MANAGER.schema and CONFIG_MANAGER.config):
        raise HTTPException(status_code=503, detail="Config/schema not loaded")

    node, path = find_service_config(CONFIG_MANAGER.config, req.process_name)
    if not node or not path:
        raise HTTPException(
            status_code=404, detail=f"process_name '{req.process_name}' not found"
        )

    path_parts: List[str] = [p for p in path.split(".") if p]
    root_props = (CONFIG_MANAGER.schema or {}).get("properties", {}) or {}
    schema_subtree: Optional[Dict[str, Any]] = find_schema(root_props, path_parts)
    if not schema_subtree:
        raise HTTPException(
            status_code=404,
            detail=f"Schema not found for config path '{path}' derived from '{req.process_name}'",
        )

    return schema_subtree


@config_router.post("/service-config")
async def handle_service_config(
    request: ServiceConfigRequest, logger=Depends(get_logger)
):
    service_name = request.service_name
    updates = request.updates
    logger.info(f"Handling config for service: {service_name}")

    service_config, service_path = find_service_config(
        CONFIG_MANAGER.config, service_name
    )

    if not service_config:
        logger.error(f"Service not found: {service_name}")
        raise HTTPException(status_code=404, detail="Service not found.")

    config_file_path = service_config.get("config_file")
    if not config_file_path:
        raise HTTPException(status_code=400, detail="No config file path defined.")

    config_path = resolve_path(config_file_path)
    if not config_path.exists():
        raise HTTPException(status_code=404, detail="Config file not found.")

    raw_config, config_data, config_format = load_config_file(config_path)

    if updates:
        try:
            save_config_file(config_path, config_data, config_format, updates)
        except Exception as e:
            logger.error(f"Failed to save config file: {e}")
            raise HTTPException(
                status_code=500, detail=f"Failed to save config file: {e}"
            )

        logger.info(f"Config for {service_name} updated successfully.")
        return {
            "status": "Config updated successfully",
            "service": service_name,
            "service_path": service_path,
        }

    logger.info(f"Config for {service_name} retrieved successfully.")
    return {
        "service": service_name,
        "service_path": service_path,
        "config_format": config_format,
        "config": config_data,
        "raw": raw_config,
    }


@config_router.get("/service-ui")
async def get_service_ui_links(
    request: Request,
    logger=Depends(get_logger),
):
    """Retrieve service UI links and generate Traefik configs dynamically."""
    try:
        traefik_config_dir = get_traefik_config_dir()
        services = ensure_ui_services_config(str(traefik_config_dir))
        services = [_normalize_direct_url(service, request) for service in services]
        traefik_config_path = traefik_config_dir / "services.yaml"

        logger.info("Updated Traefik configuration for service UIs.")
        return {
            "enabled": bool(CONFIG_MANAGER.config.get("traefik", {}).get("enabled")),
            "services": services,
            "traefik_config": str(traefik_config_path),
        }

    except Exception as e:
        logger.error(f"Failed to fetch services: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch services")


@config_router.post("/service-ui")
async def toggle_service_ui(
    request: Request,
    body: ServiceUiToggleRequest,
    process_handler=Depends(get_process_handler),
    logger=Depends(get_logger),
):
    """Enable or disable embedded service UIs and manage Traefik."""
    try:
        traefik_cfg = CONFIG_MANAGER.config.get("traefik", {})
        process_name = traefik_cfg.get("process_name", "Traefik")
        enabled = bool(body.enabled)

        traefik_cfg["enabled"] = enabled
        CONFIG_MANAGER.config["traefik"] = traefik_cfg
        CONFIG_MANAGER.save_config()

        traefik_config_dir = get_traefik_config_dir()
        services = ensure_ui_services_config(str(traefik_config_dir))
        services = [_normalize_direct_url(service, request) for service in services]
        traefik_config_path = traefik_config_dir / "services.yaml"

        if enabled:
            setup_traefik(process_handler)
            process_handler.stop_process(process_name)
            process_handler.start_process(process_name)
        else:
            process_handler.stop_process(process_name)

        return {
            "enabled": enabled,
            "services": services,
            "traefik_config": str(traefik_config_path),
        }
    except Exception as e:
        logger.error(f"Failed to toggle service UI: {e}")
        raise HTTPException(status_code=500, detail="Failed to toggle service UI")


@config_router.get("/onboarding-status")
async def onboarding_status():
    cfg = CONFIG_MANAGER.config
    return {
        "needs_onboarding": not cfg.get("dumb", {}).get("onboarding_completed", False)
    }


@config_router.post("/onboarding-completed")
async def onboarding_completed(logger=Depends(get_logger)):
    cfg = CONFIG_MANAGER.config
    cfg["dumb"]["onboarding_completed"] = True
    logger.info("Onboarding completed successfully.")
    CONFIG_MANAGER.save_config()
    return {"status": "Onboarding completed successfully"}


@config_router.post("/reset-onboarding")
async def reset_onboarding(logger=Depends(get_logger)):
    cfg = CONFIG_MANAGER.config
    cfg["dumb"]["onboarding_completed"] = False
    logger.info("Onboarding status reset to false.")
    CONFIG_MANAGER.save_config()
    return {"status": "Onboarding status reset to false"}
