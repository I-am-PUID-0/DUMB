from fastapi import APIRouter, HTTPException, Depends
from typing import Union
from pydantic import BaseModel
from utils.dependencies import get_logger, resolve_path
from utils.config_loader import CONFIG_MANAGER, find_service_config
from jsonschema import validate, ValidationError
from ruamel.yaml import YAML
from pathlib import Path
import os, json, configparser, xmltodict


class UpdateServiceConfigRequest(BaseModel):
    process_name: str
    updates: dict
    persist: bool = False


class ServiceConfigRequest(BaseModel):
    service_name: str
    updates: Union[dict, str, list] = None


config_router = APIRouter()

TRAEFIK_CONFIG_DIR = Path("/config/traefik")


def ensure_traefik_config():
    """Ensure the Traefik config directory exists."""
    TRAEFIK_CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def generate_traefik_config(services):
    """Generate a Traefik dynamic configuration for detected services."""
    traefik_config = {"http": {"routers": {}, "services": {}}}

    for service in services:
        service_name = service["name"].replace(" ", "_").lower()
        host = service["host"]
        port = service["port"]

        router_name = f"{service_name}_router"
        service_entry = f"{service_name}_service"

        traefik_config["http"]["routers"][router_name] = {
            "entryPoints": ["web"],
            "rule": f"PathPrefix(`/service/ui/{service_name}`)",
            "service": service_entry,
        }

        traefik_config["http"]["services"][service_entry] = {
            "loadBalancer": {"servers": [{"url": f"http://{host}:{port}"}]}
        }

    return traefik_config


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
                    raise HTTPException(
                        status_code=400,
                        detail="Saving updates to XML config files is not supported.",
                    )
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

    if not isinstance(config_data, str):
        raise ValueError("Expected raw string for Rclone config.")

    write_to_file(file_path, config_data)


def parse_python_config(file_path):
    exec_env = {}
    with open(file_path, "r") as file:
        exec(file.read(), {}, exec_env)
    return {k: v for k, v in exec_env.items() if not k.startswith("__")}


def write_python_config(file_path, config_data):
    validate_file_path(file_path)

    if isinstance(config_data, str):
        exec_env = {}
        exec(config_data, {}, exec_env)
        config_data = {k: v for k, v in exec_env.items() if not k.startswith("__")}

    with open(file_path, "w") as file:
        for key, value in config_data.items():
            if isinstance(value, str):
                file.write(f'{key} = "{value}"\n')
            elif isinstance(value, dict):
                file.write(f"{key} = {value}\n")
            else:
                file.write(f"{key} = {value}\n")
    validate_file_path(file_path)

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


@config_router.post("/update-dumb-config")
async def update_dumb_config(
    request: UpdateServiceConfigRequest, logger=Depends(get_logger)
):
    process_name = request.process_name
    updates = request.updates
    persist = request.persist

    logger.info(f"Received update request for {process_name} with persist={persist}")

    service_config, service_path = find_service_config(
        CONFIG_MANAGER.config, process_name
    )

    if not service_config:
        logger.error(f"Service not found: {process_name}")
        raise HTTPException(status_code=404, detail="Service not found.")

    path_parts = service_path.split(".")
    logger.debug(f"Looking up schema for path for {process_name}: {service_path}")

    instance_schema = find_schema(
        CONFIG_MANAGER.schema.get("properties", {}), path_parts
    )

    if not instance_schema:
        logger.error(f"Schema not found for process: {process_name}")
        raise HTTPException(
            status_code=400,
            detail=f"Schema not found for process: {process_name}",
        )

    logger.debug(f"Schema found for {process_name}")

    try:
        validate(instance=updates, schema=instance_schema)
        logger.debug("Validation of updates successful.")
    except ValidationError as e:
        error_path = (
            " -> ".join(map(str, e.absolute_path)) if e.absolute_path else "root"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Validation error in updates at '{error_path}': {e.message}",
        )

    try:
        temp_config = {**service_config, **updates}
        validate(instance=temp_config, schema=instance_schema)
        logger.debug("Validation of merged configuration successful.")
    except ValidationError as e:
        error_path = (
            " -> ".join(map(str, e.absolute_path)) if e.absolute_path else "root"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Validation error in merged configuration at '{error_path}': {e.message}",
        )

    try:
        for key, value in updates.items():
            if key in service_config:
                service_config[key] = value
            else:
                logger.error(f"Invalid key {key} in updates.")
                raise HTTPException(
                    status_code=400, detail=f"Invalid configuration key: {key}"
                )

        if persist:
            logger.info(f"Saving configuration for {process_name}")
            CONFIG_MANAGER.save_config(process_name=process_name)

        return {"status": "Config updated successfully", "persisted": persist}

    except Exception as e:
        logger.error(f"Failed to update configuration: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to update configuration: {str(e)}"
        )


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
async def get_service_ui_links(logger=Depends(get_logger)):
    """Retrieve service UI links and generate Traefik configs dynamically."""
    try:
        services = []

        # Ensure Traefik config directory exists
        ensure_traefik_config()

        for key, value in CONFIG_MANAGER.config.items():
            if not isinstance(value, dict):
                continue

            # Top-level service
            if value.get("enabled") and "port" in value:
                services.append(
                    {
                        "name": key,
                        "host": value.get("host", "127.0.0.1"),
                        "port": value["port"],
                    }
                )

            # Instances handling
            if "instances" in value:
                for instance_name, instance_config in value["instances"].items():
                    if instance_config.get("enabled") and "port" in instance_config:
                        services.append(
                            {
                                "name": instance_name,
                                "host": instance_config.get("host", "127.0.0.1"),
                                "port": instance_config["port"],
                            }
                        )

        # Generate and write Traefik config
        traefik_config = generate_traefik_config(services)
        traefik_config_path = TRAEFIK_CONFIG_DIR / "services.yaml"

        with open(traefik_config_path, "w") as file:
            json.dump(traefik_config, file, indent=4)

        logger.info("Updated Traefik configuration for service UIs.")
        return {"services": services, "traefik_config": str(traefik_config_path)}

    except Exception as e:
        logger.error(f"Failed to fetch services: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch services")
