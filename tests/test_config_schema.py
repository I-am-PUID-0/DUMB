import importlib
import json
import sys
import unittest
from pathlib import Path


def _load_draft7_validator():
    module = sys.modules.get("jsonschema")
    if module is not None and not hasattr(module, "Draft7Validator"):
        sys.modules.pop("jsonschema", None)
    try:
        return importlib.import_module("jsonschema").Draft7Validator
    except (AttributeError, ImportError):  # pragma: no cover - host fallback
        return None


Draft7Validator = _load_draft7_validator()


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "utils" / "dumb_config.json"
SCHEMA_PATH = ROOT / "utils" / "dumb_config_schema.json"


def _load_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


@unittest.skipIf(Draft7Validator is None, "jsonschema is not installed")
class DumbConfigSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = _load_json(CONFIG_PATH)
        cls.schema = _load_json(SCHEMA_PATH)

    def test_schema_is_valid_json_schema(self):
        Draft7Validator.check_schema(self.schema)

    def test_default_config_validates_against_schema(self):
        validator = Draft7Validator(self.schema)
        errors = sorted(
            validator.iter_errors(self.config), key=lambda error: list(error.path)
        )

        self.assertEqual(
            [],
            [
                "/" + "/".join(map(str, error.path)) + f": {error.message}"
                for error in errors
            ],
        )

    def test_source_build_services_declare_full_commit_sha_pins(self):
        service_paths = (
            ("dumb", "frontend"),
            ("traefik_proxy_admin",),
            ("cli_debrid",),
            ("decypharr",),
            ("nzbdav",),
            ("phalanx_db",),
            ("tautulli",),
            ("pulsarr",),
            ("maintainerr",),
            ("neutarr", "instances", "Default"),
            ("profilarr", "instances", "Default"),
            ("seerr", "instances", "Default"),
            ("riven_backend",),
            ("riven_frontend",),
            ("zilean",),
        )

        for path in service_paths:
            with self.subTest(path=".".join(path)):
                config_value = self.config
                schema_value = self.schema["properties"]
                for index, part in enumerate(path):
                    config_value = config_value[part]
                    if part == "instances":
                        schema_value = schema_value["instances"]["patternProperties"][
                            ".*"
                        ]["properties"]
                    elif index == 0:
                        schema_value = schema_value[part]["properties"]
                    elif path[index - 1] != "instances":
                        schema_value = schema_value[part]["properties"]

                self.assertEqual("", config_value["commit_sha"])
                self.assertEqual(
                    "^$|^[0-9a-fA-F]{40}$",
                    schema_value["commit_sha"]["pattern"],
                )

    def test_source_commit_sha_schema_rejects_short_values(self):
        invalid_config = json.loads(json.dumps(self.config))
        invalid_config["nzbdav"]["commit_sha"] = "abc1234"

        errors = list(Draft7Validator(self.schema).iter_errors(invalid_config))

        self.assertTrue(
            any(list(error.path) == ["nzbdav", "commit_sha"] for error in errors)
        )

    def test_top_level_config_keys_are_declared_and_required(self):
        config_keys = set(self.config)
        schema_keys = set(self.schema.get("properties", {}))
        required_keys = set(self.schema.get("required", []))

        self.assertEqual(config_keys, schema_keys)
        self.assertEqual(config_keys, required_keys)

    def test_instance_sections_use_pattern_properties(self):
        top_properties = self.schema.get("properties", {})
        instance_sections = sorted(
            key
            for key, value in self.config.items()
            if isinstance(value, dict) and isinstance(value.get("instances"), dict)
        )

        self.assertTrue(instance_sections)
        for key in instance_sections:
            with self.subTest(section=key):
                instances_schema = (
                    top_properties.get(key, {})
                    .get("properties", {})
                    .get("instances", {})
                )
                self.assertIn("patternProperties", instances_schema)
                self.assertTrue(instances_schema["patternProperties"])

    def test_recent_ui_sidebar_defaults_are_schema_declared(self):
        sidebar_defaults = self.config["dumb"]["ui"]["sidebar"]
        sidebar_schema = self.schema["properties"]["dumb"]["properties"]["ui"][
            "properties"
        ]["sidebar"]["properties"]

        self.assertEqual(set(sidebar_defaults), set(sidebar_schema))

    def test_rclone_direct_provider_credentials_are_defaulted_and_schema_declared(
        self,
    ):
        default_instance = next(iter(self.config["rclone"]["instances"].values()))
        instance_schema = next(
            iter(
                self.schema["properties"]["rclone"]["properties"]["instances"][
                    "patternProperties"
                ].values()
            )
        )["properties"]

        for key in ("username", "password", "customer_id"):
            with self.subTest(key=key):
                self.assertIn(key, default_instance)
                self.assertIn(key, instance_schema)

    def test_altmount_defaults_are_schema_declared(self):
        altmount_defaults = self.config["altmount"]
        altmount_schema = self.schema["properties"]["altmount"]["properties"]

        for key in (
            "enabled",
            "process_name",
            "repo_owner",
            "repo_name",
            "pinned_version",
            "mount_type",
            "port",
            "config_dir",
            "config_file",
            "metadata_dir",
            "mount_path",
            "log_file",
            "command",
            "env",
        ):
            with self.subTest(key=key):
                self.assertIn(key, altmount_defaults)
                self.assertIn(key, altmount_schema)

    def test_bazarr_config_file_matches_current_data_root_layout(self):
        self.assertEqual(
            self.config["bazarr"]["config_file"],
            "/bazarr/data/config/config.yaml",
        )


if __name__ == "__main__":
    unittest.main()
