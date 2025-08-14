import os, shutil, copy
from json import load, dump, JSONDecodeError
from jsonschema import validate, ValidationError
from dotenv import load_dotenv, find_dotenv
from collections import OrderedDict


class ConfigManager:
    def __init__(
        self,
        file_path="/config/dumb_config.json",
        schema_path="/utils/dumb_config_schema.json",
    ):
        if not os.path.exists(file_path):
            shutil.copyfile("/utils/dumb_config.json", file_path)

        load_dotenv(find_dotenv("/config/.env"))

        self.file_path = os.path.abspath(file_path)
        self.schema_path = os.path.abspath(schema_path)

        if not os.path.exists(self.file_path):
            raise FileNotFoundError(f"Config file not found: {self.file_path}")
        if not os.path.exists(self.schema_path):
            raise FileNotFoundError(f"Schema file not found: {self.schema_path}")

        self.update_config_with_defaults()
        # self.update_config_with_top_level_defaults()
        self.schema = self._load_schema()
        self.config = self._load_and_validate_config()

    def _load_schema(self):
        with open(self.schema_path, "r") as schema_file:
            return load(schema_file)

    def _load_config(self):
        try:
            with open(self.file_path, "r") as config_file:
                return load(config_file)
        except JSONDecodeError as e:
            raise ValueError(
                f"JSON syntax error in {self.file_path}: {e.msg} at line {e.lineno}, column {e.colno}"
            )

    def _prune_extraneous_keys(self, config, reference):
        if not isinstance(config, dict) or not isinstance(reference, dict):
            return config
        if reference == {}:
            return config
        pruned = OrderedDict()

        for key, ref_val in reference.items():
            if key == "instances" and isinstance(ref_val, dict):
                default_template = next(iter(ref_val.values()))
                instances = config.get("instances", {})
                pruned_instances = OrderedDict()

                for inst_name, inst_cfg in instances.items():
                    pruned_instances[inst_name] = self._prune_extraneous_keys(
                        inst_cfg, default_template
                    )

                pruned["instances"] = pruned_instances
                continue

            if key in config:
                cfg_val = config[key]
                if isinstance(cfg_val, dict) and isinstance(ref_val, dict):
                    pruned[key] = self._prune_extraneous_keys(cfg_val, ref_val)
                else:
                    pruned[key] = cfg_val

        return pruned

    def update_config_with_top_level_defaults(self):
        try:
            with open("/utils/dumb_config.json", "r") as default_file:
                default_config = load(default_file, object_pairs_hook=OrderedDict)

            existing_config = self._load_config()
            updated_config = OrderedDict()
            updated = False

            for key in default_config:
                if key in existing_config and existing_config[key]:
                    updated_config[key] = existing_config[key]
                else:
                    updated_config[key] = default_config[key]
                    updated = True

            for key in existing_config:
                if key not in updated_config:
                    updated_config[key] = existing_config[key]

            if updated:
                with open(self.file_path, "w") as config_file:
                    dump(updated_config, config_file, indent=4)
        except Exception as e:
            raise ValueError(f"Error during update_config_with_top_level_defaults: {e}")

    def update_config_with_defaults(self):
        try:
            with open("/utils/dumb_config.json", "r") as default_file:
                default_config = load(default_file, object_pairs_hook=OrderedDict)

            existing_config = self._load_config()

            merged_config = self._merge_configs(
                copy.deepcopy(existing_config), default_config
            )

            pruned_config = self._prune_extraneous_keys(merged_config, default_config)

            if pruned_config != existing_config:
                backup_path = self.file_path + ".bak"
                shutil.copyfile(self.file_path, backup_path)
                with open(self.file_path, "w") as config_file:
                    dump(pruned_config, config_file, indent=4)

            self.config = pruned_config
        except Exception as e:
            raise ValueError(f"Error during update_config_with_defaults: {e}")

    def _merge_configs(self, existing, default):
        if not isinstance(existing, dict) or not isinstance(default, dict):
            return existing

        merged = OrderedDict()

        for key, default_value in default.items():
            if key == "instances" and isinstance(default_value, dict):
                existing_instances = existing.get("instances", {})
                merged_instances = OrderedDict()
                default_template = next(iter(default_value.values()))

                if existing_instances:
                    for instance_name, instance_value in existing_instances.items():
                        merged_instances[instance_name] = self._merge_configs(
                            instance_value, default_template
                        )
                else:
                    merged_instances = default_value

                merged["instances"] = merged_instances
                continue

            if key in existing:
                existing_value = existing[key]
                if isinstance(existing_value, dict) and isinstance(default_value, dict):
                    merged[key] = self._merge_configs(existing_value, default_value)
                else:
                    merged[key] = existing_value
            else:
                merged[key] = default_value

        for key, existing_value in existing.items():
            if key not in merged:
                merged[key] = existing_value

        return merged

    def _load_and_validate_config(self):
        config = self._load_config()
        try:
            validate(instance=config, schema=self.schema)
        except ValidationError as e:
            error_path = (
                " -> ".join(map(str, e.absolute_path)) if e.absolute_path else "root"
            )
            raise ValueError(
                f"Configuration validation error at '{error_path}': {e.message}"
            )
        return self._merge_with_env(config)

    def _load_and_merge_config(self):
        config = self._load_config()
        return self._merge_with_env(config)

    def _merge_with_env(self, config, prefix=None):
        for key, value in config.items():
            current_keys = prefix + [key] if prefix else [key]

            if isinstance(value, dict):
                config[key] = self._merge_with_env(value, current_keys)
            else:
                # print(f"settings.items: Key: {key}, Default: {value}")
                env_value = self._get_env_var(current_keys)
                # print(f"Key: {key}, Value: {env_value}")
                normalized_value = self._normalize_value(key.lower(), env_value, value)
                # print(f"Normalized Value: {normalized_value}")
                config[key] = self._validate_value(key.lower(), normalized_value)

        return config

    def _get_env_var(self, keys):
        env_var = "_".join([str(k).upper() for k in keys])
        secret_file = f"/run/secrets/{env_var}"

        try:
            with open(secret_file, "r") as file:
                return file.read().strip()
        except IOError:
            pass

        value = os.getenv(env_var)
        # print(f"_get_env_var Value for {env_var}: {value}")

        return value if value and value.strip() != "" else None

    def _normalize_value(self, key, value, default):
        if value is None:
            return default

        if key in ["log_level", "loglevel"]:
            return value.strip().upper()

        if isinstance(default, bool):
            return value.lower() in ["true", "1", "yes"]

        if isinstance(default, str):
            return value.strip()

        return self._cast_value(value, default)

    def _validate_value(self, key, value):
        if key == "log_level":
            valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
            if value not in valid_levels:
                raise ValueError(f"Invalid log level: {value}")
        return value

    def _cast_value(self, value, default):
        if value is None:
            return default

        try:
            if isinstance(default, bool):
                return value.lower() in ["true", "1", "yes"]
            elif isinstance(default, int):
                return int(value)
            elif isinstance(default, float):
                return float(value)
            return value
        except ValueError:
            return default

    def fix_null_strings(data: dict, schema: dict):
        if not isinstance(data, dict) or not isinstance(schema, dict):
            return

        props = schema.get("properties", {})
        for key, val in list(data.items()):
            if key in props:
                sch = props[key]
                if sch.get("type") == "string" and val is None:
                    data[key] = ""
                elif isinstance(val, dict):
                    ConfigManager.fix_null_strings(val, sch)
                elif key == "instances" and isinstance(val, dict):
                    template_schema = next(iter(sch["properties"].values()))
                    for inst in val.values():
                        ConfigManager.fix_null_strings(inst, template_schema)

    def save_config(self, process_name=None):
        if process_name:
            section_config = find_service_config(self.config, process_name)
            if not section_config:
                raise ValueError(f"Process {process_name} does not exist in config.")

            with open(self.file_path, "r") as config_file:
                full_config = load(config_file)

            def update_nested_config(data, target_name, updated_config):
                if isinstance(data, dict):
                    for key, value in data.items():
                        if (
                            isinstance(value, dict)
                            and value.get("process_name") == target_name
                        ):
                            ConfigManager.fix_null_strings(updated_config, self.schema)
                            data[key] = updated_config
                            return True
                        if update_nested_config(value, target_name, updated_config):
                            return True
                return False

            if not update_nested_config(full_config, process_name, section_config):
                raise ValueError(f"Failed to locate process {process_name} in file.")

            with open(self.file_path, "w") as config_file:
                dump(full_config, config_file, indent=4)
        else:
            ConfigManager.fix_null_strings(self.config, self.schema)
            with open(self.file_path, "w") as config_file:
                dump(self.config, config_file, indent=4)

    def get(self, key, section=None, normalize_case=False):
        value = (
            self.config.get(section, {}).get(key) if section else self.config.get(key)
        )

        if normalize_case and isinstance(value, str):
            return value.lower()

        return value

    def get_instance(self, instance_name=None, key=None):
        if instance_name:
            config = self.get(key).get("instances").get(instance_name)
        elif key and key == "dumb_frontend" or key == "dumb_api_service":
            section, key = key.split("_")
            config = self.get(key, section)
        else:
            config = self.get(key)
        return config

    def set(self, section, key, value):
        if section:
            if section not in self.config:
                self.config[section] = {}
            self.config[section][key] = value
        else:
            self.config[key] = value

    def reload(self):
        self.config = self._load_and_merge_config()

    def find_key_for_process(self, process_name):
        for key, value in self.config.items():
            if isinstance(value, dict) and value.get("process_name") == process_name:
                return key, None

            if isinstance(value, dict) and "instances" in value:
                for instance_name, instance_config in value["instances"].items():
                    if instance_config.get("process_name") == process_name:
                        return key, instance_name

            if key == "dumb" and isinstance(value, dict):
                for subkey, subvalue in value.items():
                    if (
                        isinstance(subvalue, dict)
                        and subvalue.get("process_name") == process_name
                    ):
                        return key + "_" + subkey, None

        return None, None


def find_service_config(config, process_name):
    if isinstance(config, dict):
        for key, value in config.items():
            if isinstance(value, dict) and value.get("process_name") == process_name:
                return value
            found = find_service_config(value, process_name)
            if found:
                return found
    return None


CONFIG_MANAGER = ConfigManager()
