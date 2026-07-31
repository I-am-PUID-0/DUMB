import sys
import types
import unittest


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class _ConfigManager:
    def __init__(self):
        self.config = {}

    def get(self, key, default=None):
        return self.config.get(key, default)


def _install_runtime_stubs():
    config_loader = types.ModuleType("utils.config_loader")
    config_loader.CONFIG_MANAGER = _ConfigManager()
    sys.modules["utils.config_loader"] = config_loader

    download = types.ModuleType("utils.download")
    download.Downloader = lambda *args, **kwargs: object()
    sys.modules["utils.download"] = download

    global_logger = types.ModuleType("utils.global_logger")
    global_logger.logger = _Logger()
    sys.modules["utils.global_logger"] = global_logger

    versions = types.ModuleType("utils.versions")
    versions.Versions = lambda *args, **kwargs: object()
    sys.modules["utils.versions"] = versions

    yaml = types.ModuleType("yaml")
    sys.modules["yaml"] = yaml

    ruamel = types.ModuleType("ruamel")
    ruamel_yaml = types.ModuleType("ruamel.yaml")
    ruamel_yaml.YAML = lambda *args, **kwargs: object()
    sys.modules["ruamel"] = ruamel
    sys.modules["ruamel.yaml"] = ruamel_yaml


_install_runtime_stubs()

sys.modules.pop("utils.traefik_setup", None)
from utils import traefik_setup


class TraefikSetupHelperTests(unittest.TestCase):
    def setUp(self):
        traefik_setup.CONFIG_MANAGER.config = {}

    def test_normalize_version_adds_v_prefix_and_lowercases(self):
        self.assertEqual(traefik_setup._normalize_version("3.3.6"), "v3.3.6")
        self.assertEqual(traefik_setup._normalize_version(" V3.3.6 "), "v3.3.6")
        self.assertIsNone(traefik_setup._normalize_version("  "))
        self.assertIsNone(traefik_setup._normalize_version(None))

    def test_parse_entrypoint_port_uses_trailing_port_or_fallback(self):
        self.assertEqual(traefik_setup._parse_entrypoint_port(":18080", 80), 18080)
        self.assertEqual(
            traefik_setup._parse_entrypoint_port("127.0.0.1:19090", 80), 19090
        )
        self.assertEqual(traefik_setup._parse_entrypoint_port("web", 80), 80)
        self.assertEqual(traefik_setup._parse_entrypoint_port(":bad", 80), 80)

    def test_prepare_entrypoints_allows_only_encoded_slashes_by_default(self):
        configured = {"web": {"address": ":18080"}}

        result = traefik_setup._prepare_entrypoints(configured)

        self.assertEqual(
            result["web"]["http"]["encodedCharacters"],
            {"allowEncodedSlash": True},
        )
        self.assertNotIn("http", configured["web"])

    def test_prepare_entrypoints_preserves_explicit_encoded_slash_hardening(self):
        result = traefik_setup._prepare_entrypoints(
            {
                "web": {
                    "address": ":18080",
                    "http": {"encodedCharacters": {"allowEncodedSlash": False}},
                }
            }
        )

        self.assertFalse(
            result["web"]["http"]["encodedCharacters"]["allowEncodedSlash"]
        )

    def test_resolve_traefik_service_uses_dashboard_api_port(self):
        traefik_setup.CONFIG_MANAGER.config = {
            "traefik": {"entrypoints": {"web": {"address": ":18080"}}}
        }

        services = traefik_setup._resolve_ui_service(
            {
                "name": "traefik",
                "config_key": "traefik",
                "path": "/dashboard/",
                "path_prefix": "/dashboard",
                "internal_service": "api@internal",
            }
        )

        self.assertEqual(services[0]["port"], 18081)
        self.assertEqual(services[0]["internal_service"], "api@internal")

    def test_resolve_dumb_subkey_service_normalizes_wildcard_host(self):
        traefik_setup.CONFIG_MANAGER.config = {
            "dumb": {
                "api_service": {
                    "enabled": True,
                    "host": "0.0.0.0",
                    "port": 8000,
                    "process_name": "DUMB API",
                }
            }
        }

        services = traefik_setup._resolve_ui_service(
            {"name": "dumb_api_service", "config_key": "dumb", "subkey": "api_service"}
        )

        self.assertEqual(services[0]["host"], "127.0.0.1")
        self.assertEqual(services[0]["process_name"], "DUMB API")

    def test_resolve_multi_instance_service_returns_only_enabled_instances(self):
        traefik_setup.CONFIG_MANAGER.config = {
            "sonarr": {
                "host": "0.0.0.0",
                "instances": {
                    "main": {
                        "enabled": True,
                        "port": 8989,
                        "process_name": "Sonarr Main",
                    },
                    "disabled": {
                        "enabled": False,
                        "port": 8990,
                        "process_name": "Sonarr Disabled",
                    },
                },
            }
        }

        services = traefik_setup._resolve_ui_service(
            {"name": "sonarr", "config_key": "sonarr"}
        )

        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["name"], "Sonarr Main")
        self.assertEqual(services[0]["host"], "127.0.0.1")
        self.assertEqual(services[0]["port"], 8989)

    def test_build_ui_services_includes_enabled_authelia_portal(self):
        traefik_setup.CONFIG_MANAGER.config = {
            "authelia": {
                "enabled": True,
                "host": "0.0.0.0",
                "port": 9091,
                "process_name": "Authelia",
                "public_url": "https://auth.example.com",
            }
        }

        services = traefik_setup.build_ui_services()

        authelia = next(
            service for service in services if service["config_key"] == "authelia"
        )
        self.assertEqual(authelia["name"], "authelia")
        self.assertEqual(authelia["process_name"], "Authelia")
        self.assertEqual(authelia["host"], "127.0.0.1")
        self.assertEqual(authelia["port"], 9091)
        self.assertEqual(authelia["public_url"], "https://auth.example.com")

        generated = traefik_setup.generate_traefik_config([authelia])
        self.assertEqual(
            generated["http"]["middlewares"]["authelia_public_origin"],
            {
                "headers": {
                    "customRequestHeaders": {
                        "X-Forwarded-Host": "auth.example.com",
                        "X-Forwarded-Proto": "https",
                        "X-Forwarded-Ssl": "on",
                    }
                }
            },
        )
        self.assertEqual(
            generated["http"]["routers"]["authelia_router"]["middlewares"],
            ["authelia_strip", "authelia_public_origin", "ui_frame_headers"],
        )

    def test_authelia_embedded_route_ignores_unsafe_public_origin(self):
        generated = traefik_setup.generate_traefik_config(
            [
                {
                    "name": "authelia",
                    "config_key": "authelia",
                    "host": "127.0.0.1",
                    "port": 9091,
                    "public_url": "http://localhost:9091",
                }
            ]
        )

        self.assertNotIn("authelia_public_origin", generated["http"]["middlewares"])


if __name__ == "__main__":
    unittest.main()
