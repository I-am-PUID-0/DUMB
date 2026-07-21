import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from scripts import generate_env_example

ROOT = Path(__file__).resolve().parents[1]


def _load_config_manager_class():
    source = (ROOT / "utils" / "config_loader.py").read_text(encoding="utf-8")
    source = source.rsplit("CONFIG_MANAGER = ConfigManager()", 1)[0]
    saved_modules = {name: sys.modules.get(name) for name in ("jsonschema", "dotenv")}

    jsonschema = types.ModuleType("jsonschema")
    jsonschema.validate = lambda *args, **kwargs: None
    jsonschema.ValidationError = type("ValidationError", (Exception,), {})
    sys.modules["jsonschema"] = jsonschema

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    dotenv.find_dotenv = lambda *args, **kwargs: ""
    sys.modules["dotenv"] = dotenv

    try:
        module = types.ModuleType("config_loader_under_test")
        exec(compile(source, "utils/config_loader.py", "exec"), module.__dict__)
        return module.ConfigManager
    finally:
        for name, module in saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class EnvExampleGeneratorTests(unittest.TestCase):
    def test_generate_env_example_flattens_scalars_and_collections(self):
        generated = generate_env_example.generate_env_example(
            {
                "puid": 1000,
                "dumb": {
                    "frontend": {
                        "process_name": "DUMB Frontend",
                        "origins": ["http://0.0.0.0:3005"],
                    },
                    "ui": {"sidebar": {"service_shortcuts": {}}},
                },
            }
        )

        self.assertIn("PUID=1000", generated)
        self.assertIn('DUMB_FRONTEND_PROCESS_NAME="DUMB Frontend"', generated)
        self.assertIn('DUMB_FRONTEND_ORIGINS=["http://0.0.0.0:3005"]', generated)
        self.assertIn("DUMB_UI_SIDEBAR_SERVICE_SHORTCUTS={}", generated)

    def test_env_example_is_in_sync_with_dumb_config(self):
        config = json.loads((ROOT / "utils" / "dumb_config.json").read_text())
        expected = generate_env_example.generate_env_example(config)
        current = (ROOT / ".env.example").read_text()

        self.assertEqual(current, expected)


class ConfigLoaderEnvParsingTests(unittest.TestCase):
    def setUp(self):
        config_manager = _load_config_manager_class()
        self.manager = config_manager.__new__(config_manager)

    def test_normalize_value_parses_json_lists_and_dicts(self):
        self.assertEqual(
            self.manager._normalize_value("origins", '["http://localhost:3005"]', []),
            ["http://localhost:3005"],
        )
        self.assertEqual(
            self.manager._normalize_value("env", '{"PORT":"3004"}', {}),
            {"PORT": "3004"},
        )

    def test_normalize_value_keeps_default_for_invalid_collections(self):
        self.assertEqual(
            self.manager._normalize_value(
                "origins", "http://localhost:3005", ["default"]
            ),
            ["default"],
        )
        self.assertEqual(
            self.manager._normalize_value("env", "[]", {"PORT": "3004"}),
            {"PORT": "3004"},
        )


class ConfigLoaderMigrationTests(unittest.TestCase):
    def test_bazarr_legacy_config_path_is_persisted(self):
        config_manager = _load_config_manager_class()
        with tempfile.TemporaryDirectory() as temp_dir:
            config = json.loads((ROOT / "utils" / "dumb_config.json").read_text())
            config["bazarr"]["config_file"] = "/bazarr/data/config.yaml"
            config_path = Path(temp_dir) / "dumb_config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            manager = config_manager(
                file_path=str(config_path),
                schema_path=str(ROOT / "utils" / "dumb_config_schema.json"),
            )

            persisted = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                manager.get("bazarr")["config_file"],
                "/bazarr/data/config/config.yaml",
            )
            self.assertEqual(
                persisted["bazarr"]["config_file"],
                "/bazarr/data/config/config.yaml",
            )


if __name__ == "__main__":
    unittest.main()
