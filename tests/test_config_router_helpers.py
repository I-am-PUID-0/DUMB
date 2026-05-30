import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_runtime_stubs():
    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=500, detail=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class APIRouter:
        def get(self, *args, **kwargs):
            return lambda func: func

        def post(self, *args, **kwargs):
            return lambda func: func

        def put(self, *args, **kwargs):
            return lambda func: func

    fastapi.APIRouter = APIRouter
    fastapi.HTTPException = HTTPException
    fastapi.Depends = lambda *args, **kwargs: None
    fastapi.Query = lambda *args, **kwargs: None
    fastapi.Request = object
    sys.modules["fastapi"] = fastapi

    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = type("BaseModel", (), {})
    sys.modules["pydantic"] = pydantic

    dependencies = types.ModuleType("utils.dependencies")
    dependencies.get_logger = lambda: None
    dependencies.get_process_handler = lambda: None
    dependencies.resolve_path = lambda path: path
    dependencies.get_optional_current_user = lambda: None
    sys.modules["utils.dependencies"] = dependencies

    config_loader = types.ModuleType("utils.config_loader")
    config_loader.CONFIG_MANAGER = types.SimpleNamespace(config={})
    config_loader.find_service_config = lambda *args, **kwargs: (None, None)
    sys.modules["utils.config_loader"] = config_loader

    traefik_setup = types.ModuleType("utils.traefik_setup")
    for name in (
        "ensure_ui_services_config",
        "get_traefik_config_dir",
        "get_traefik_dynamic_config_dir",
        "setup_traefik",
        "build_ui_services",
    ):
        setattr(traefik_setup, name, lambda *args, **kwargs: None)
    sys.modules["utils.traefik_setup"] = traefik_setup

    jsonschema = types.ModuleType("jsonschema")
    jsonschema.validate = lambda *args, **kwargs: None
    jsonschema.ValidationError = type("ValidationError", (Exception,), {})
    sys.modules["jsonschema"] = jsonschema

    ruamel = types.ModuleType("ruamel")
    ruamel_yaml = types.ModuleType("ruamel.yaml")
    ruamel_yaml.YAML = lambda *args, **kwargs: types.SimpleNamespace(
        load=lambda raw: {},
        dump=lambda data, file: file.write(str(data)),
        indent=lambda *args, **kwargs: None,
        preserve_quotes=False,
    )
    sys.modules["ruamel"] = ruamel
    sys.modules["ruamel.yaml"] = ruamel_yaml

    xmltodict = types.ModuleType("xmltodict")
    xmltodict.parse = lambda raw: {}
    xmltodict.unparse = lambda data, pretty=True: ""
    sys.modules["xmltodict"] = xmltodict


_install_runtime_stubs()

from api.routers import config as config_router


class _ConfigManager:
    def __init__(self, config, schema):
        self.config = config
        self.schema = schema
        self.saved_process_names = []

    def save_config(self, process_name=None):
        self.saved_process_names.append(process_name)

    def find_key_for_process(self, process_name):
        return "sonarr", None


class _Logger:
    def __init__(self):
        self.errors = []
        self.infos = []
        self.warnings = []

    def error(self, message, *args):
        self.errors.append(message % args if args else message)

    def info(self, message, *args):
        self.infos.append(message % args if args else message)

    def warning(self, message, *args):
        self.warnings.append(message % args if args else message)


class _Request:
    def __init__(self, scheme="https", host="dumb.example", forwarded_host=None):
        headers = {"host": host}
        if forwarded_host:
            headers["x-forwarded-host"] = forwarded_host
        self.headers = headers
        self.url = types.SimpleNamespace(scheme=scheme)


def _service_schema():
    return {
        "properties": {
            "sonarr": {
                "properties": {
                    "instances": {
                        "patternProperties": {
                            ".*": {
                                "properties": {
                                    "process_name": {"type": "string"},
                                    "port": {"type": "integer"},
                                    "schema_declared": {"type": "boolean"},
                                }
                            }
                        }
                    }
                }
            }
        }
    }


class ConfigRouterHelperTests(unittest.TestCase):
    def test_deep_merge_dict_preserves_sibling_nested_keys(self):
        target = {
            "dumb": {"ui": {"log_timestamp": True, "sidebar": {"compact": False}}}
        }
        updates = {"dumb": {"ui": {"sidebar": {"compact": True}}}}

        result = config_router._deep_merge_dict(target, updates)

        self.assertIs(result, target)
        self.assertEqual(
            target,
            {"dumb": {"ui": {"log_timestamp": True, "sidebar": {"compact": True}}}},
        )

    def test_normalize_direct_url_rewrites_local_service_host_to_request_host(self):
        service = {
            "direct_url": "http://localhost:8989/",
            "host": "0.0.0.0",
            "port": 8989,
        }

        result = config_router._normalize_direct_url(service, _Request())

        self.assertIs(result, service)
        self.assertEqual(service["direct_url"], "https://dumb.example:8989/")

    def test_normalize_direct_url_prefers_forwarded_host_without_port(self):
        service = {
            "direct_url": "http://127.0.0.1:7878/",
            "host": "127.0.0.1",
            "port": 7878,
        }

        result = config_router._normalize_direct_url(
            service, _Request(scheme="http", forwarded_host="public.example:443")
        )

        self.assertIs(result, service)
        self.assertEqual(service["direct_url"], "http://public.example:7878/")

    def test_normalize_direct_url_preserves_locked_or_remote_urls(self):
        locked = {
            "direct_url": "http://localhost:9696/",
            "direct_url_locked": True,
            "host": "localhost",
            "port": 9696,
        }
        remote = {
            "direct_url": "http://service.lan:5055/",
            "host": "service.lan",
            "port": 5055,
        }

        self.assertEqual(
            config_router._normalize_direct_url(locked, _Request())["direct_url"],
            "http://localhost:9696/",
        )
        self.assertEqual(
            config_router._normalize_direct_url(remote, _Request())["direct_url"],
            "http://service.lan:5055/",
        )

    def test_find_service_config_finds_nested_instances_and_paths(self):
        config = {
            "sonarr": {
                "instances": {
                    "default": {"process_name": "Sonarr Default"},
                    "anime": {"process_name": "Sonarr Anime"},
                }
            },
            "group": {"child": {"process_name": "Nested Service"}},
        }

        instance, path = config_router.find_service_config(config, "Sonarr Anime")
        nested, nested_path = config_router.find_service_config(
            config, "Nested Service"
        )

        self.assertEqual(instance, {"process_name": "Sonarr Anime"})
        self.assertEqual(path, "sonarr.instances.anime")
        self.assertEqual(nested, {"process_name": "Nested Service"})
        self.assertEqual(nested_path, "group.child")

    def test_find_schema_walks_properties_and_pattern_properties(self):
        schema = {
            "properties": {
                "sonarr": {
                    "properties": {
                        "instances": {
                            "patternProperties": {
                                ".*": {"properties": {"port": {"type": "integer"}}}
                            }
                        }
                    }
                }
            }
        }

        self.assertEqual(
            config_router.find_schema(
                schema, ["sonarr", "instances", "default", "port"]
            ),
            {"type": "integer"},
        )
        self.assertIsNone(config_router.find_schema(schema, ["radarr"]))

    def test_parse_postgresql_conf_supports_equals_and_space_separated_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "postgresql.conf")
            path.write_text(
                "# comment\nshared_buffers = 256MB\nmax_connections 100\n\n"
            )

            lines, parsed = config_router.parse_postgresql_conf(path)

        self.assertEqual(len(lines), 4)
        self.assertEqual(parsed, {"shared_buffers": "256MB", "max_connections": "100"})

    def test_parse_ini_and_rclone_config_preserve_expected_option_behavior(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ini_path = Path(temp_dir, "service.ini")
            rclone_path = Path(temp_dir, "rclone.conf")
            ini_path.write_text("[Section]\nMixedCase = value\npercent = 100%\n")
            rclone_path.write_text(
                "[remote]\ntype = webdav\nurl = http://example.invalid\n"
            )

            ini_data, ini_raw = config_router.parse_ini_config(ini_path)
            rclone_data, rclone_raw = config_router.parse_rclone_config(rclone_path)

        self.assertEqual(ini_data["Section"]["MixedCase"], "value")
        self.assertEqual(ini_data["Section"]["percent"], "100%")
        self.assertIn("MixedCase", ini_raw)
        self.assertEqual(rclone_data["remote"]["type"], "webdav")
        self.assertEqual(rclone_data["remote"]["url"], "http://example.invalid")
        self.assertIn("[remote]", rclone_raw)

    def test_parse_python_config_ignores_dunder_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "settings.py")
            path.write_text("PORT = 8080\nNAME = 'service'\n__secret__ = 'hidden'\n")

            parsed = config_router.parse_python_config(path)

        self.assertEqual(parsed, {"PORT": 8080, "NAME": "service"})

    def test_parse_python_config_does_not_execute_non_literal_assignments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "settings.py")
            marker = Path(temp_dir, "marker")
            path.write_text(
                "SAFE = {'port': 8080}\n"
                f"UNSAFE = open({str(marker)!r}, 'w').write('executed')\n"
            )

            parsed = config_router.parse_python_config(path)

        self.assertEqual(parsed, {"SAFE": {"port": 8080}})
        self.assertFalse(marker.exists())

    def test_update_config_global_deep_merges_and_persists(self):
        manager = _ConfigManager(
            {
                "dumb": {
                    "ui": {
                        "log_timestamp": True,
                        "sidebar": {"compact_mode": False, "tools_open": True},
                    }
                }
            },
            {},
        )
        config_router.CONFIG_MANAGER = manager
        request = types.SimpleNamespace(
            process_name=None,
            updates={"dumb": {"ui": {"sidebar": {"compact_mode": True}}}},
        )

        result = asyncio.run(config_router.update_config(request, logger=_Logger()))

        self.assertEqual(result, {"status": "global config updated", "keys": ["dumb"]})
        self.assertEqual(manager.saved_process_names, [None])
        self.assertEqual(
            manager.config,
            {
                "dumb": {
                    "ui": {
                        "log_timestamp": True,
                        "sidebar": {"compact_mode": True, "tools_open": True},
                    }
                }
            },
        )

    def test_update_config_service_allows_schema_declared_new_keys(self):
        manager = _ConfigManager(
            {
                "sonarr": {
                    "instances": {"default": {"process_name": "Sonarr", "port": 8989}}
                }
            },
            _service_schema(),
        )
        config_router.CONFIG_MANAGER = manager
        request = types.SimpleNamespace(
            process_name="Sonarr", updates={"schema_declared": True}, persist=False
        )

        result = asyncio.run(config_router.update_config(request, logger=_Logger()))

        self.assertEqual(
            result,
            {
                "status": "service config updated",
                "process_name": "Sonarr",
                "persisted": False,
            },
        )
        self.assertTrue(
            manager.config["sonarr"]["instances"]["default"]["schema_declared"]
        )
        self.assertEqual(manager.saved_process_names, [])

    def test_update_config_service_rejects_keys_outside_config_schema_and_dynamic_set(
        self,
    ):
        manager = _ConfigManager(
            {
                "sonarr": {
                    "instances": {"default": {"process_name": "Sonarr", "port": 8989}}
                }
            },
            _service_schema(),
        )
        config_router.CONFIG_MANAGER = manager
        logger = _Logger()
        request = types.SimpleNamespace(
            process_name="Sonarr", updates={"unknown_key": True}, persist=False
        )

        with self.assertRaises(config_router.HTTPException) as ctx:
            asyncio.run(config_router.update_config(request, logger=logger))

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Invalid configuration key: unknown_key")
        self.assertNotIn(
            "unknown_key", manager.config["sonarr"]["instances"]["default"]
        )
        self.assertEqual(manager.saved_process_names, [])


if __name__ == "__main__":
    unittest.main()
