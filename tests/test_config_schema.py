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


if __name__ == "__main__":
    unittest.main()
