import asyncio
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from pathlib import Path

import xmltodict as real_xmltodict


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
    xmltodict.unparse = lambda data, **kwargs: ""
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


def _validate_schema_types(instance, schema):
    expected_type = schema.get("type")
    type_map = {
        "object": dict,
        "boolean": bool,
        "string": str,
        "integer": int,
        "number": (int, float),
    }
    if expected_type and not isinstance(instance, type_map[expected_type]):
        raise config_router.ValidationError(
            f"{instance!r} is not of type {expected_type!r}"
        )

    if isinstance(instance, dict):
        for key, sub_schema in (schema.get("properties") or {}).items():
            if key in instance:
                _validate_schema_types(instance[key], sub_schema)


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
    def setUp(self):
        for helper in (
            "infinidysk_postgres_migration_active",
            "infinidysk_namespace_migration_active",
        ):
            active_patch = patch.object(config_router, helper, return_value=False)
            active_patch.start()
            self.addCleanup(active_patch.stop)
        validator_patch = patch.object(
            config_router,
            "_reject_infinidysk_postgres_reversal",
            return_value=None,
        )
        validator_patch.start()
        self.addCleanup(validator_patch.stop)

    @staticmethod
    def _infinidysk_schema():
        return {
            "properties": {
                "infinidysk": {
                    "type": "object",
                    "properties": {
                        "process_name": {"type": "string"},
                        "postgres_enabled": {"type": "boolean"},
                        "postgres_database": {"type": "string"},
                        "config_dir": {"type": "string"},
                        "env": {"type": "object"},
                    },
                },
                "postgres": {"type": "object"},
            }
        }

    def test_legacy_nzbdav_global_update_normalizes_to_infinidysk(self):
        payload = {
            "nzbdav": {"enabled": True, "config_dir": "/nzbdav"},
        }

        result = config_router._normalize_legacy_global_config(payload)

        self.assertNotIn("nzbdav", result)
        self.assertTrue(result["infinidysk"]["enabled"])
        self.assertEqual("/nzbdav", result["infinidysk"]["config_dir"])

    def test_conflicting_service_identities_are_rejected(self):
        payload = {
            "nzbdav": {"enabled": True},
            "infinidysk": {"enabled": False},
        }

        with self.assertRaisesRegex(Exception, "will not guess"):
            config_router._normalize_legacy_global_config(payload)

    def test_redact_notification_secrets_returns_copy(self):
        original = {
            "dumb": {
                "notifications": {
                    "destinations": [
                        {
                            "id": "ops",
                            "url": "https://example.invalid/secret",
                            "headers": {"Authorization": "secret"},
                        }
                    ]
                }
            }
        }

        result = config_router._redact_notification_secrets(original)
        destination = result["dumb"]["notifications"]["destinations"][0]

        self.assertEqual(destination["url"], "")
        self.assertEqual(destination["headers"], {})
        self.assertNotIn("url_configured", destination)
        self.assertNotIn("headers_configured", destination)
        self.assertEqual(
            original["dumb"]["notifications"]["destinations"][0]["url"],
            "https://example.invalid/secret",
        )

    def test_redacted_notification_round_trip_preserves_stored_secrets(self):
        current = {
            "dumb": {
                "notifications": {
                    "destinations": [
                        {
                            "id": "ops",
                            "url": "https://example.invalid/secret",
                            "headers": {"Authorization": "secret"},
                        }
                    ]
                }
            }
        }
        updates = config_router._redact_notification_secrets(current)

        result = config_router._preserve_redacted_notification_secrets(updates, current)
        destination = result["dumb"]["notifications"]["destinations"][0]

        self.assertEqual(destination["url"], "https://example.invalid/secret")
        self.assertEqual(destination["headers"], {"Authorization": "secret"})

    def test_notification_secret_preservation_strips_dedicated_api_markers(self):
        current = {
            "dumb": {
                "notifications": {
                    "destinations": [
                        {
                            "id": "ops",
                            "url": "https://example.invalid/secret",
                            "headers": {"Authorization": "secret"},
                        }
                    ]
                }
            }
        }
        updates = config_router._redact_notification_secrets(current)
        destination = updates["dumb"]["notifications"]["destinations"][0]
        destination["url_configured"] = True
        destination["headers_configured"] = True

        result = config_router._preserve_redacted_notification_secrets(updates, current)
        destination = result["dumb"]["notifications"]["destinations"][0]

        self.assertNotIn("url_configured", destination)
        self.assertNotIn("headers_configured", destination)

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

    def test_parse_postgresql_conf_decodes_quotes_and_ignores_inline_comments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "postgresql.conf")
            path.write_text(
                "dynamic_shared_memory_type = posix\t# provider #1\n"
                "log_timezone = 'Etc/UTC'\n"
                "datestyle = 'iso, mdy'\n"
                "custom_setting = 'value # retained' # actual comment\n"
                "escaped_quote = 'operator''s value'\n"
            )

            _, parsed = config_router.parse_postgresql_conf(path)

        self.assertEqual(parsed["dynamic_shared_memory_type"], "posix")
        self.assertEqual(parsed["log_timezone"], "Etc/UTC")
        self.assertEqual(parsed["datestyle"], "iso, mdy")
        self.assertEqual(parsed["custom_setting"], "value # retained")
        self.assertEqual(parsed["escaped_quote"], "operator's value")

    def test_write_postgresql_conf_full_round_trip_is_byte_for_byte_lossless(self):
        original = (
            "# PostgreSQL configuration\r\n"
            "port = 5432\r\n"
            "dynamic_shared_memory_type = posix\t# provider default\r\n"
            "log_timezone = 'Etc/UTC'\r\n"
            "datestyle = 'iso, mdy'\r\n"
            "lc_messages = 'C.UTF-8'\t# locale for errors\r\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "postgresql.conf")
            path.write_bytes(original.encode())
            path.chmod(0o640)
            _, parsed = config_router.parse_postgresql_conf(path)

            config_router.write_postgresql_conf(path, parsed)

            self.assertEqual(path.read_bytes(), original.encode())
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_write_postgresql_conf_changes_only_requested_values(self):
        original = (
            "# PostgreSQL configuration\n"
            "max_wal_size = 1GB\t\t# upper WAL bound\n"
            "min_wal_size = 80MB\n"
            "log_timezone = 'Etc/UTC'\n"
            "datestyle = 'iso, mdy'\n"
        )
        expected = (
            "# PostgreSQL configuration\n"
            "max_wal_size = 4GB\t\t# upper WAL bound\n"
            "min_wal_size = 1GB\n"
            "log_timezone = 'Etc/UTC'\n"
            "datestyle = 'iso, mdy'\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "postgresql.conf")
            path.write_text(original)
            _, parsed = config_router.parse_postgresql_conf(path)
            parsed.update({"max_wal_size": "4GB", "min_wal_size": "1GB"})

            config_router.write_postgresql_conf(path, parsed)

            self.assertEqual(path.read_text(), expected)

    def test_write_postgresql_conf_accepts_legacy_quoted_editor_values_once(self):
        original = "log_timezone = 'Etc/UTC'\nmax_connections = 100\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "postgresql.conf")
            path.write_text(original)

            config_router.write_postgresql_conf(
                path,
                {"log_timezone": "'Etc/UTC'", "max_connections": "'150'"},
            )

            self.assertEqual(
                path.read_text(),
                "log_timezone = 'Etc/UTC'\nmax_connections = 150\n",
            )

    def test_write_postgresql_conf_rejects_invalid_candidate_without_changes(self):
        original = "log_timezone = 'Etc/UTC'\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "postgresql.conf")
            path.write_text(original)

            with self.assertRaises(config_router.HTTPException) as raised:
                config_router.write_postgresql_conf(
                    path, "log_timezone = ''Etc/UTC''\n"
                )

            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(path.read_text(), original)

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

    def test_update_config_global_normalizes_legacy_riven_wait_for_dir(self):
        manager = _ConfigManager(
            {
                "dumb": {
                    "ui": {
                        "geek_mode": False,
                        "sidebar": {"compact_mode": False},
                    }
                },
                "riven_backend": {
                    "enabled": False,
                    "wait_for_dir": None,
                },
            },
            {
                "properties": {
                    "dumb": {
                        "type": "object",
                        "properties": {
                            "ui": {
                                "type": "object",
                                "properties": {
                                    "geek_mode": {"type": "boolean"},
                                    "sidebar": {
                                        "type": "object",
                                        "properties": {
                                            "compact_mode": {"type": "boolean"}
                                        },
                                    },
                                },
                            }
                        },
                    },
                    "riven_backend": {
                        "type": "object",
                        "properties": {
                            "enabled": {"type": "boolean"},
                            "wait_for_dir": {"type": "string"},
                        },
                    },
                }
            },
        )
        config_router.CONFIG_MANAGER = manager
        request = types.SimpleNamespace(
            process_name=None,
            updates={"dumb": {"ui": {"geek_mode": True}}},
        )
        original_validate = config_router.validate
        try:
            config_router.validate = _validate_schema_types

            result = asyncio.run(config_router.update_config(request, logger=_Logger()))
        finally:
            config_router.validate = original_validate

        self.assertEqual(result, {"status": "global config updated", "keys": ["dumb"]})
        self.assertEqual(manager.saved_process_names, [None])
        self.assertEqual(manager.config["dumb"]["ui"]["geek_mode"], True)
        self.assertEqual(manager.config["riven_backend"]["wait_for_dir"], "")

    def test_update_config_global_rejects_unknown_root_keys_when_schema_present(self):
        manager = _ConfigManager(
            {
                "dumb": {
                    "ui": {"log_timestamp": True},
                }
            },
            {
                "properties": {
                    "dumb": {
                        "type": "object",
                        "properties": {
                            "ui": {
                                "type": "object",
                                "properties": {
                                    "log_timestamp": {"type": "boolean"},
                                },
                            }
                        },
                    }
                }
            },
        )
        config_router.CONFIG_MANAGER = manager
        request = types.SimpleNamespace(
            process_name=None,
            updates={"evil": {"enabled": True}},
        )

        with self.assertRaises(config_router.HTTPException) as ctx:
            asyncio.run(config_router.update_config(request, logger=_Logger()))

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "Invalid global configuration key: evil")
        self.assertEqual(
            manager.config,
            {
                "dumb": {
                    "ui": {"log_timestamp": True},
                }
            },
        )

    def test_update_config_global_rejects_schema_violations(self):
        manager = _ConfigManager(
            {
                "dumb": {
                    "ui": {"log_timestamp": True},
                }
            },
            {
                "properties": {
                    "dumb": {
                        "type": "object",
                        "properties": {
                            "ui": {
                                "type": "object",
                                "properties": {
                                    "log_timestamp": {"type": "boolean"},
                                },
                            }
                        },
                    }
                }
            },
        )
        config_router.CONFIG_MANAGER = manager
        original_validate = config_router.validate
        try:

            def fake_validate(instance, schema):
                if instance["dumb"]["ui"].get("log_timestamp") not in (True, False):
                    raise config_router.ValidationError("validation failed")

            config_router.validate = fake_validate

            request = types.SimpleNamespace(
                process_name=None,
                updates={"dumb": {"ui": {"log_timestamp": "not-a-bool"}}},
            )

            with self.assertRaises(config_router.HTTPException) as ctx:
                asyncio.run(config_router.update_config(request, logger=_Logger()))

            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("Validation error in global update", ctx.exception.detail)
            self.assertEqual(
                manager.config,
                {
                    "dumb": {
                        "ui": {"log_timestamp": True},
                    }
                },
            )
        finally:
            config_router.validate = original_validate

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

    def test_service_update_rejects_infinidysk_postgres_reversal_before_mutation(self):
        service = {
            "process_name": "InfiniDysk",
            "postgres_enabled": True,
            "config_dir": "/infinidysk",
            "env": {"DATABASE_PROVIDER": "postgres"},
        }
        manager = _ConfigManager(
            {"infinidysk": service, "postgres": {}},
            {
                "properties": {
                    "infinidysk": {
                        "type": "object",
                        "properties": {
                            "process_name": {"type": "string"},
                            "postgres_enabled": {"type": "boolean"},
                            "config_dir": {"type": "string"},
                            "env": {"type": "object"},
                        },
                    }
                }
            },
        )
        manager.find_key_for_process = lambda _: ("infinidysk", None)
        config_router.CONFIG_MANAGER = manager
        request = types.SimpleNamespace(
            process_name="InfiniDysk",
            updates={"postgres_enabled": False},
            persist=True,
        )

        with (
            patch.object(
                config_router,
                "find_service_config",
                return_value=(service, "infinidysk"),
            ),
            patch.object(
                config_router,
                "_reject_infinidysk_postgres_reversal",
                side_effect=config_router.HTTPException(
                    status_code=400, detail="explicit rollback required"
                ),
            ),
            self.assertRaises(config_router.HTTPException) as context,
        ):
            asyncio.run(config_router.update_config(request, logger=_Logger()))

        self.assertEqual(context.exception.status_code, 400)
        self.assertTrue(service["postgres_enabled"])
        self.assertEqual(manager.saved_process_names, [])

    def test_global_update_rejects_infinidysk_reversal_before_deep_merge(self):
        manager = _ConfigManager(
            {
                "infinidysk": {
                    "process_name": "InfiniDysk",
                    "postgres_enabled": True,
                    "config_dir": "/infinidysk",
                    "env": {"DATABASE_PROVIDER": "postgres"},
                },
                "postgres": {},
            },
            {},
        )
        config_router.CONFIG_MANAGER = manager
        request = types.SimpleNamespace(
            process_name=None,
            updates={"infinidysk": {"postgres_enabled": False}},
        )

        with (
            patch.object(
                config_router,
                "_reject_infinidysk_postgres_reversal",
                side_effect=config_router.HTTPException(
                    status_code=400, detail="explicit rollback required"
                ),
            ),
            self.assertRaises(config_router.HTTPException) as context,
        ):
            asyncio.run(config_router.update_config(request, logger=_Logger()))

        self.assertEqual(context.exception.status_code, 400)
        self.assertTrue(manager.config["infinidysk"]["postgres_enabled"])
        self.assertEqual(manager.saved_process_names, [])

    def test_service_update_rejects_postgres_infinidysk_rename_before_mutation(self):
        service = {
            "process_name": "InfiniDysk",
            "postgres_enabled": True,
            "config_dir": "/infinidysk",
            "env": {"DATABASE_PROVIDER": "postgres"},
        }
        schema = {
            "properties": {
                "infinidysk": {
                    "type": "object",
                    "properties": {
                        "process_name": {"type": "string"},
                        "postgres_enabled": {"type": "boolean"},
                        "config_dir": {"type": "string"},
                        "env": {"type": "object"},
                    },
                }
            }
        }
        manager = _ConfigManager({"infinidysk": service, "postgres": {}}, schema)
        manager.find_key_for_process = lambda _: ("infinidysk", None)
        config_router.CONFIG_MANAGER = manager
        request = types.SimpleNamespace(
            process_name="InfiniDysk",
            updates={"process_name": "Renamed InfiniDysk"},
            persist=True,
        )

        with (
            patch.object(
                config_router,
                "find_service_config",
                return_value=(service, "infinidysk"),
            ),
            patch.object(
                config_router,
                "_reject_infinidysk_postgres_reversal",
                side_effect=config_router.HTTPException(
                    status_code=400, detail="guarded identity is fixed"
                ),
            ),
            self.assertRaises(config_router.HTTPException) as context,
        ):
            asyncio.run(config_router.update_config(request, logger=_Logger()))

        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual("InfiniDysk", service["process_name"])
        self.assertEqual(manager.saved_process_names, [])

    def test_global_update_rejects_postgres_infinidysk_rename_before_merge(self):
        manager = _ConfigManager(
            {
                "infinidysk": {
                    "process_name": "InfiniDysk",
                    "postgres_enabled": True,
                    "config_dir": "/infinidysk",
                    "env": {"DATABASE_PROVIDER": "postgres"},
                },
                "postgres": {},
            },
            {},
        )
        config_router.CONFIG_MANAGER = manager
        request = types.SimpleNamespace(
            process_name=None,
            updates={"infinidysk": {"process_name": "Renamed InfiniDysk"}},
        )

        with (
            patch.object(
                config_router,
                "_reject_infinidysk_postgres_reversal",
                side_effect=config_router.HTTPException(
                    status_code=400, detail="guarded identity is fixed"
                ),
            ),
            self.assertRaises(config_router.HTTPException) as context,
        ):
            asyncio.run(config_router.update_config(request, logger=_Logger()))

        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual("InfiniDysk", manager.config["infinidysk"]["process_name"])
        self.assertEqual(manager.saved_process_names, [])

    def test_service_update_rejects_existing_sqlite_postgres_enable_without_save(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "db.sqlite").write_bytes(b"existing sqlite")
            service = {
                "process_name": "InfiniDysk",
                "postgres_enabled": False,
                "postgres_database": "infinidysk",
                "config_dir": temp_dir,
                "env": {"CONFIG_PATH": temp_dir, "DATABASE_PROVIDER": "sqlite"},
            }
            manager = _ConfigManager(
                {"infinidysk": service, "postgres": {}},
                self._infinidysk_schema(),
            )
            manager.find_key_for_process = lambda _: ("infinidysk", None)
            config_router.CONFIG_MANAGER = manager
            request = types.SimpleNamespace(
                process_name="InfiniDysk",
                updates={"postgres_enabled": True},
                persist=True,
            )

            with (
                patch.object(
                    config_router,
                    "find_service_config",
                    return_value=(service, "infinidysk"),
                ),
                patch.object(
                    config_router,
                    "_reject_infinidysk_postgres_reversal",
                    side_effect=config_router.HTTPException(
                        status_code=400, detail="existing SQLite data"
                    ),
                ),
                self.assertRaises(config_router.HTTPException) as context,
            ):
                asyncio.run(config_router.update_config(request, logger=_Logger()))

            self.assertEqual(context.exception.status_code, 400)
            self.assertFalse(service["postgres_enabled"])
            self.assertEqual(manager.saved_process_names, [])

    def test_service_update_rejects_managed_provider_env_before_save(self):
        service = {
            "process_name": "InfiniDysk",
            "postgres_enabled": False,
            "config_dir": "/infinidysk",
            "env": {"DATABASE_PROVIDER": "sqlite"},
        }
        manager = _ConfigManager(
            {"infinidysk": service, "postgres": {}},
            self._infinidysk_schema(),
        )
        manager.find_key_for_process = lambda _: ("infinidysk", None)
        config_router.CONFIG_MANAGER = manager
        request = types.SimpleNamespace(
            process_name="InfiniDysk",
            updates={
                "env": {
                    "DATABASE_PROVIDER": "postgres",
                    "DATABASE_CONNECTION_STRING": "Host=example.invalid",
                }
            },
            persist=True,
        )

        with (
            patch.object(
                config_router,
                "find_service_config",
                return_value=(service, "infinidysk"),
            ),
            patch.object(
                config_router,
                "_reject_infinidysk_postgres_reversal",
                side_effect=config_router.HTTPException(
                    status_code=400, detail="DATABASE_PROVIDER is managed by DUMB"
                ),
            ),
            self.assertRaises(config_router.HTTPException) as context,
        ):
            asyncio.run(config_router.update_config(request, logger=_Logger()))

        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual(service["env"], {"DATABASE_PROVIDER": "sqlite"})
        self.assertEqual(manager.saved_process_names, [])

    def test_service_update_can_clear_unapplied_postgres_flag(self):
        service = {
            "process_name": "InfiniDysk",
            "postgres_enabled": True,
            "postgres_database": "infinidysk",
            "config_dir": "/infinidysk",
            "env": {"DATABASE_PROVIDER": "sqlite"},
        }
        manager = _ConfigManager(
            {"infinidysk": service, "postgres": {}},
            self._infinidysk_schema(),
        )
        manager.find_key_for_process = lambda _: ("infinidysk", None)
        config_router.CONFIG_MANAGER = manager
        request = types.SimpleNamespace(
            process_name="InfiniDysk",
            updates={"postgres_enabled": False},
            persist=True,
        )

        with patch.object(
            config_router,
            "find_service_config",
            return_value=(service, "infinidysk"),
        ):
            asyncio.run(config_router.update_config(request, logger=_Logger()))

        self.assertFalse(service["postgres_enabled"])
        self.assertEqual(manager.saved_process_names, ["InfiniDysk"])

    def test_active_migration_blocks_service_and_global_changes_before_save(self):
        service = {
            "process_name": "InfiniDysk",
            "postgres_enabled": False,
            "config_dir": "/infinidysk",
            "env": {"DATABASE_PROVIDER": "sqlite"},
        }
        manager = _ConfigManager(
            {"infinidysk": service, "postgres": {"host": "127.0.0.1"}},
            self._infinidysk_schema(),
        )
        manager.find_key_for_process = lambda _: ("infinidysk", None)
        config_router.CONFIG_MANAGER = manager

        with (
            patch.object(
                config_router,
                "find_service_config",
                return_value=(service, "infinidysk"),
            ),
            patch.object(
                config_router,
                "infinidysk_postgres_migration_active",
                return_value=True,
            ),
        ):
            with self.assertRaises(config_router.HTTPException) as service_error:
                asyncio.run(
                    config_router.update_config(
                        types.SimpleNamespace(
                            process_name="InfiniDysk",
                            updates={"config_dir": "/different"},
                            persist=True,
                        ),
                        logger=_Logger(),
                    )
                )
            with self.assertRaises(config_router.HTTPException) as global_error:
                asyncio.run(
                    config_router.update_config(
                        types.SimpleNamespace(
                            process_name=None,
                            updates={"postgres": {"host": "example.invalid"}},
                        ),
                        logger=_Logger(),
                    )
                )

        self.assertEqual(service_error.exception.status_code, 409)
        self.assertEqual(global_error.exception.status_code, 409)
        self.assertEqual(service["config_dir"], "/infinidysk")
        self.assertEqual(manager.config["postgres"]["host"], "127.0.0.1")
        self.assertEqual(manager.saved_process_names, [])

    def test_active_namespace_migration_blocks_postgres_change_before_save(self):
        service = {
            "process_name": "InfiniDysk",
            "postgres_enabled": False,
            "config_dir": "/infinidysk",
            "env": {"DATABASE_PROVIDER": "sqlite"},
        }
        manager = _ConfigManager(
            {"infinidysk": service, "postgres": {"host": "127.0.0.1"}},
            self._infinidysk_schema(),
        )
        config_router.CONFIG_MANAGER = manager

        with (
            patch.object(
                config_router,
                "infinidysk_namespace_migration_active",
                return_value=True,
            ),
            self.assertRaises(config_router.HTTPException) as raised,
        ):
            asyncio.run(
                config_router.update_config(
                    types.SimpleNamespace(
                        process_name=None,
                        updates={"postgres": {"host": "example.invalid"}},
                    ),
                    logger=_Logger(),
                )
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(manager.config["postgres"]["host"], "127.0.0.1")
        self.assertEqual(manager.saved_process_names, [])

    def test_active_namespace_migration_blocks_all_config_changes_before_save(self):
        sonarr = {
            "process_name": "Sonarr TV",
            "port": 8989,
            "schema_declared": False,
        }
        schema = _service_schema()
        schema["properties"]["dumb"] = {
            "type": "object",
            "properties": {"label": {"type": "string"}},
        }
        manager = _ConfigManager(
            {
                "sonarr": {"instances": {"TV": sonarr}},
                "dumb": {"label": "before"},
            },
            schema,
        )
        config_router.CONFIG_MANAGER = manager

        with (
            patch.object(
                config_router,
                "find_service_config",
                return_value=(sonarr, "sonarr.instances.TV"),
            ),
            patch.object(
                config_router,
                "infinidysk_namespace_migration_active",
                return_value=True,
            ),
        ):
            with self.assertRaises(config_router.HTTPException) as service_error:
                asyncio.run(
                    config_router.update_config(
                        types.SimpleNamespace(
                            process_name="Sonarr TV",
                            updates={"port": 8990},
                            persist=True,
                        ),
                        logger=_Logger(),
                    )
                )
            with self.assertRaises(config_router.HTTPException) as global_error:
                asyncio.run(
                    config_router.update_config(
                        types.SimpleNamespace(
                            process_name=None,
                            updates={"dumb": {"label": "after"}},
                        ),
                        logger=_Logger(),
                    )
                )

        self.assertEqual(service_error.exception.status_code, 409)
        self.assertEqual(global_error.exception.status_code, 409)
        self.assertEqual(sonarr["port"], 8989)
        self.assertEqual(manager.config["dumb"]["label"], "before")
        self.assertEqual(manager.saved_process_names, [])

    def test_cutover_binding_rejection_is_pre_save_and_password_rotation_is_allowed(
        self,
    ):
        service = {
            "process_name": "InfiniDysk",
            "postgres_enabled": True,
            "postgres_database": "infinidysk",
            "config_dir": "/infinidysk",
            "env": {"DATABASE_PROVIDER": "postgres"},
        }
        manager = _ConfigManager(
            {
                "infinidysk": service,
                "postgres": {
                    "host": "127.0.0.1",
                    "config_dir": "/postgres_data",
                    "password": "old",
                },
            },
            self._infinidysk_schema(),
        )
        manager.find_key_for_process = lambda _: ("infinidysk", None)
        config_router.CONFIG_MANAGER = manager

        with patch.object(
            config_router,
            "_reject_infinidysk_postgres_reversal",
            side_effect=config_router.HTTPException(
                status_code=400, detail="cutover binding changed"
            ),
        ):
            with self.assertRaises(config_router.HTTPException):
                asyncio.run(
                    config_router.update_config(
                        types.SimpleNamespace(
                            process_name=None,
                            updates={"postgres": {"config_dir": "/other-cluster"}},
                        ),
                        logger=_Logger(),
                    )
                )
        self.assertEqual(manager.config["postgres"]["config_dir"], "/postgres_data")
        self.assertEqual(manager.saved_process_names, [])

        with patch.object(
            config_router,
            "_reject_infinidysk_postgres_reversal",
            return_value=None,
        ):
            asyncio.run(
                config_router.update_config(
                    types.SimpleNamespace(
                        process_name=None,
                        updates={"postgres": {"password": "rotated"}},
                    ),
                    logger=_Logger(),
                )
            )
        self.assertEqual(manager.config["postgres"]["password"], "rotated")
        self.assertEqual(manager.saved_process_names, [None])

    def test_process_scoped_postgres_update_is_guarded_before_save(self):
        postgres = {
            "process_name": "PostgreSQL",
            "host": "127.0.0.1",
            "password": "old",
            "config_dir": "/postgres_data",
        }
        manager = _ConfigManager(
            {
                "infinidysk": {
                    "process_name": "InfiniDysk",
                    "postgres_enabled": True,
                    "config_dir": "/infinidysk",
                    "env": {"DATABASE_PROVIDER": "postgres"},
                },
                "postgres": postgres,
            },
            self._infinidysk_schema(),
        )
        manager.find_key_for_process = lambda _: ("postgres", None)
        config_router.CONFIG_MANAGER = manager

        with (
            patch.object(
                config_router,
                "find_service_config",
                return_value=(postgres, "postgres"),
            ),
            patch.object(
                config_router,
                "_reject_infinidysk_postgres_reversal",
                side_effect=config_router.HTTPException(
                    status_code=400, detail="target binding changed"
                ),
            ),
            self.assertRaises(config_router.HTTPException),
        ):
            asyncio.run(
                config_router.update_config(
                    types.SimpleNamespace(
                        process_name="PostgreSQL",
                        updates={"config_dir": "/other-cluster"},
                        persist=True,
                    ),
                    logger=_Logger(),
                )
            )
        self.assertEqual(postgres["config_dir"], "/postgres_data")
        self.assertEqual(manager.saved_process_names, [])

        with (
            patch.object(
                config_router,
                "find_service_config",
                return_value=(postgres, "postgres"),
            ),
            patch.object(
                config_router,
                "_reject_infinidysk_postgres_reversal",
                return_value=None,
            ),
        ):
            asyncio.run(
                config_router.update_config(
                    types.SimpleNamespace(
                        process_name="PostgreSQL",
                        updates={"password": "rotated"},
                        persist=True,
                    ),
                    logger=_Logger(),
                )
            )
        self.assertEqual(postgres["password"], "rotated")
        self.assertEqual(manager.saved_process_names, ["PostgreSQL"])

    def test_load_config_file_uses_safe_yaml_parser(self):
        created = []

        class FakeYAML:
            def __init__(self, typ=None):
                created.append(typ)

            def load(self, raw):
                return {"loaded": True}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "service.yaml")
            path.write_text("a: 1\n")

            with patch.object(config_router, "YAML", FakeYAML):
                _, config_data, _ = config_router.load_config_file(path)

        self.assertEqual(config_data, {"loaded": True})
        self.assertIn("safe", created)

    def test_save_config_file_updates_with_safe_yaml_parser(self):
        created = []

        class FakeYAML:
            def __init__(self, typ=None):
                created.append(typ)
                self.typ = typ

            def load(self, raw):
                return {"from_updates": True}

            def indent(self, *args, **kwargs):
                return None

            def dump(self, data, file):
                file.write(str(data))

        def fake_write_to_file(path, data):
            self.fail("write_to_file should not be used for yaml updates")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "service.yaml")
            path.write_text("{}\n")

            with (
                patch.object(config_router, "YAML", FakeYAML),
                patch.object(config_router, "write_to_file", fake_write_to_file),
            ):
                config_router.save_config_file(path, {}, "yaml", updates="a: 2")

        self.assertIn("safe", created)

    def test_save_xml_accepts_stringified_json_and_preserves_compact_layout(self):
        original = '<Preferences FriendlyName="Before" MachineIdentifier="example"/>\n'

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "Preferences.xml")
            path.write_text(original, encoding="utf-8")
            updates = (
                "{\n"
                '  "Preferences": {\n'
                '    "@FriendlyName": "After",\n'
                '    "@MachineIdentifier": "example"\n'
                "  }\n"
                "}"
            )

            with patch.object(config_router, "xmltodict", real_xmltodict):
                _, config_data, config_format = config_router.load_config_file(path)
                config_router.save_config_file(
                    path, config_data, config_format, updates=updates
                )

            rendered = path.read_text(encoding="utf-8")
            parsed = real_xmltodict.parse(rendered)

        self.assertEqual(parsed["Preferences"]["@FriendlyName"], "After")
        self.assertEqual(parsed["Preferences"]["@MachineIdentifier"], "example")
        self.assertEqual(len(rendered.splitlines()), 1)
        self.assertTrue(rendered.rstrip().endswith("/>"))

    def test_save_xml_rejects_invalid_text_without_overwriting_file(self):
        original = '<Preferences FriendlyName="Before"/>\n'

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "Preferences.xml")
            path.write_text(original, encoding="utf-8")

            with (
                patch.object(config_router, "xmltodict", real_xmltodict),
                self.assertRaises(config_router.HTTPException) as ctx,
            ):
                _, config_data, config_format = config_router.load_config_file(path)
                config_router.save_config_file(
                    path, config_data, config_format, updates="{not valid"
                )

            rendered = path.read_text(encoding="utf-8")

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(rendered, original)

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

    def test_global_config_redacts_media_protection_api_keys(self):
        source = {
            "dumb": {
                "media_protection": {
                    "services": [
                        {
                            "process_name": "Jellyfin Media Server",
                            "api_key": "jellyfin-secret",
                        }
                    ]
                }
            }
        }

        safe = config_router._redact_notification_secrets(source)

        entry = safe["dumb"]["media_protection"]["services"][0]
        self.assertEqual(entry["api_key"], "")
        self.assertTrue(entry["api_key_configured"])
        self.assertEqual(
            source["dumb"]["media_protection"]["services"][0]["api_key"],
            "jellyfin-secret",
        )

    def test_redacted_media_key_is_preserved_on_global_round_trip(self):
        current = {
            "dumb": {
                "media_protection": {
                    "services": [
                        {
                            "process_name": "Jellyfin Media Server",
                            "api_key": "jellyfin-secret",
                        }
                    ]
                }
            }
        }
        updates = {
            "dumb": {
                "media_protection": {
                    "services": [
                        {
                            "process_name": "Jellyfin Media Server",
                            "api_key": "",
                            "api_key_configured": True,
                            "enabled": False,
                        }
                    ]
                }
            }
        }

        safe = config_router._preserve_redacted_media_protection_secrets(
            updates, current
        )

        entry = safe["dumb"]["media_protection"]["services"][0]
        self.assertEqual(entry["api_key"], "jellyfin-secret")
        self.assertNotIn("api_key_configured", entry)
        self.assertFalse(entry["enabled"])

    def test_service_config_write_is_frozen_by_migration_admission(self):
        cases = (
            ("postgres", "PostgreSQL", False, True),
            ("sonarr", "Sonarr", True, False),
        )
        for config_key, process_name, namespace_active, postgres_active in cases:
            with (
                self.subTest(config_key=config_key),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                config_path = Path(temp_dir) / "service.json"
                original = '{"setting": "before"}\n'
                config_path.write_text(original, encoding="utf-8")
                manager = types.SimpleNamespace(
                    config={
                        config_key: {
                            "process_name": process_name,
                            "config_file": str(config_path),
                        }
                    }
                )
                request = types.SimpleNamespace(
                    service_name=process_name,
                    updates={"setting": "after"},
                )
                with (
                    patch.object(config_router, "CONFIG_MANAGER", manager),
                    patch.object(
                        config_router,
                        "resolve_path",
                        side_effect=lambda value: Path(value),
                    ),
                    patch.object(
                        config_router,
                        "infinidysk_namespace_migration_active",
                        return_value=namespace_active,
                    ),
                    patch.object(
                        config_router,
                        "infinidysk_postgres_migration_active",
                        return_value=postgres_active,
                    ),
                    patch.object(config_router, "save_config_file") as save,
                    self.assertRaises(config_router.HTTPException) as ctx,
                ):
                    asyncio.run(
                        config_router.handle_service_config(
                            request,
                            logger=_Logger(),
                        )
                    )

                self.assertEqual(409, ctx.exception.status_code)
                self.assertEqual(original, config_path.read_text(encoding="utf-8"))
                save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
