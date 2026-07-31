import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import setup


class TraefikProxyAdminSetupTests(unittest.TestCase):
    def test_install_phase_persists_generated_integration_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            (config_dir / ".next").mkdir()
            (config_dir / "package.json").write_text("{}\n", encoding="utf-8")
            (config_dir / ".next" / "BUILD_ID").write_text(
                "test-build\n", encoding="utf-8"
            )
            (config_dir / "version.txt").write_text(
                "commit-123456789abc\n", encoding="utf-8"
            )
            config = {
                "enabled": True,
                "process_name": "Traefik Proxy Admin",
                "config_dir": str(config_dir),
                "port": 3004,
                "env": {"ADMIN_AUTH_SECRET": "a" * 48},
            }

            def get_config(key):
                return {
                    "traefik_proxy_admin": config,
                    "traefik": {
                        "enabled": True,
                        "entrypoints": {"web": {"address": ":18080"}},
                    },
                    "postgres": {
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": 5432,
                    },
                    "authelia": {"enabled": False},
                    "puid": 1000,
                    "pgid": 1000,
                }.get(key)

            with (
                patch.object(setup.CONFIG_MANAGER, "get", side_effect=get_config),
                patch.object(setup.CONFIG_MANAGER, "save_config") as save_config,
                patch.object(setup, "_ensure_traefik_enabled", return_value=False),
                patch.object(setup, "_ensure_postgres_enabled", return_value=False),
                patch.object(
                    setup, "_ensure_postgres_database_config", return_value=False
                ),
                patch.object(
                    setup,
                    "_initialize_postgres_databases_if_running",
                    return_value=(True, None),
                ),
                patch.object(
                    setup,
                    "_postgres_database_url",
                    return_value="postgresql://example.invalid/traefik_proxy_admin",
                ),
                patch.object(
                    setup, "_patch_traefik_proxy_admin_for_dumb", return_value=False
                ),
                patch.object(
                    setup,
                    "_sync_traefik_proxy_admin_standalone_assets",
                    return_value=False,
                ),
                patch.object(setup, "_chown_recursive_if_needed"),
                patch.object(setup.os, "makedirs"),
                patch.dict(os.environ, {"PATH": "/usr/bin"}),
            ):
                success, error = setup.setup_traefik_proxy_admin(
                    object(), install_only=True
                )

            self.assertTrue(success, error)
            self.assertGreaterEqual(len(config["env"]["DUMB_INTEGRATION_TOKEN"]), 48)
            save_config.assert_called_once_with("Traefik Proxy Admin")


if __name__ == "__main__":
    unittest.main()
