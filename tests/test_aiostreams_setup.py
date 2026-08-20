import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import setup


class AIOStreamsSetupTests(unittest.TestCase):
    def _config(self, config_dir: str) -> dict:
        return {
            "enabled": True,
            "process_name": "AIOStreams",
            "release_version_enabled": False,
            "release_version": "latest",
            "base_url": "http://localhost:3006",
            "auth_username": "admin",
            "auth_password": "correct-horse-battery-staple",
            "secret_key": "",
            "database_uri": "sqlite://./data/db.sqlite",
            "port": 3010,
            "config_dir": config_dir,
            "command": [],
            "env": {"CUSTOM_AIOSTREAMS_SETTING": "preserved"},
        }

    def test_configure_generates_secret_once_and_syncs_managed_runtime_env(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            generated = "ab" * 32
            with (
                patch.object(setup.CONFIG_MANAGER, "get", return_value=config),
                patch.object(setup.CONFIG_MANAGER, "save_config") as save_config,
                patch.object(setup, "aiostreams_runtime_ready", return_value=True),
                patch.object(
                    setup, "aiostreams_runtime_matches_selection", return_value=True
                ),
                patch.object(setup.secrets, "token_hex", return_value=generated),
                patch.object(setup, "chown_single"),
                patch.object(setup, "chown_recursive", return_value=(True, None)),
            ):
                success, error = setup.setup_aiostreams(object(), configure_only=True)
                self.assertTrue(success, error)
                save_config.assert_called_once_with("AIOStreams")

                self.assertEqual(generated, config["secret_key"])
                self.assertEqual("http://localhost:3010", config["base_url"])
                self.assertEqual(generated, config["env"]["SECRET_KEY"])
                self.assertEqual(
                    "admin:correct-horse-battery-staple",
                    config["env"]["AIOSTREAMS_AUTH"],
                )
                self.assertEqual(
                    "admin=admin", config["env"]["AIOSTREAMS_AUTH_PERMISSIONS"]
                )
                self.assertEqual("3010", config["env"]["PORT"])
                self.assertEqual("http://127.0.0.1:3010", config["env"]["INTERNAL_URL"])
                self.assertEqual(
                    "sqlite://./data/db.sqlite", config["env"]["DATABASE_URI"]
                )
                self.assertEqual(str(Path(temp_dir) / "data"), config["env"]["HOME"])
                self.assertEqual(
                    str(Path(temp_dir) / "runtime" / "lib" / "libmimalloc.so.2"),
                    config["env"]["LD_PRELOAD"],
                )
                self.assertEqual(
                    "preserved", config["env"]["CUSTOM_AIOSTREAMS_SETTING"]
                )
                self.assertEqual(
                    [
                        "node",
                        str(
                            Path(temp_dir)
                            / "runtime"
                            / "packages"
                            / "server"
                            / "dist"
                            / "server.js"
                        ),
                    ],
                    config["command"],
                )

                save_config.reset_mock()
                success, error = setup.setup_aiostreams(object(), configure_only=True)
                self.assertTrue(success, error)
                save_config.assert_not_called()
                self.assertEqual(generated, config["secret_key"])

    def test_configure_preserves_explicit_public_base_url_and_postgres_uri(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            config.update(
                {
                    "base_url": "https://streams.example.com/app",
                    "secret_key": "cd" * 32,
                    "database_uri": "postgres://user:secret@db:5432/aiostreams",
                }
            )
            with (
                patch.object(setup.CONFIG_MANAGER, "get", return_value=config),
                patch.object(setup.CONFIG_MANAGER, "save_config") as save_config,
                patch.object(setup, "aiostreams_runtime_ready", return_value=True),
                patch.object(
                    setup, "aiostreams_runtime_matches_selection", return_value=True
                ),
                patch.object(setup, "chown_single"),
                patch.object(setup, "chown_recursive", return_value=(True, None)),
            ):
                success, error = setup.setup_aiostreams(object(), configure_only=True)

            self.assertTrue(success, error)
            save_config.assert_not_called()
            self.assertEqual(
                "https://streams.example.com/app", config["env"]["BASE_URL"]
            )
            self.assertEqual(config["database_uri"], config["env"]["DATABASE_URI"])

    def test_configure_rejects_non_loopback_http_base_url(self):
        config = self._config("/aiostreams")
        config.update(
            {
                "base_url": "http://streams.example.com/app",
                "secret_key": "cd" * 32,
            }
        )
        with (
            patch.object(setup.CONFIG_MANAGER, "get", return_value=config),
            patch.object(setup.CONFIG_MANAGER, "save_config") as save_config,
            patch.object(setup, "aiostreams_runtime_ready", return_value=True),
            patch.object(
                setup, "aiostreams_runtime_matches_selection", return_value=True
            ),
        ):
            success, error = setup.setup_aiostreams(object(), configure_only=True)

        self.assertFalse(success)
        self.assertIn("must use HTTPS unless", error)
        save_config.assert_not_called()

    def test_configure_requires_dashboard_credentials(self):
        config = self._config("/aiostreams")
        config["auth_password"] = ""
        with (
            patch.object(setup.CONFIG_MANAGER, "get", return_value=config),
            patch.object(setup.CONFIG_MANAGER, "save_config") as save_config,
            patch.object(setup, "aiostreams_runtime_ready", return_value=True),
            patch.object(
                setup, "aiostreams_runtime_matches_selection", return_value=True
            ),
        ):
            success, error = setup.setup_aiostreams(object(), configure_only=True)

        self.assertFalse(success)
        self.assertIn("required for dashboard login", error)
        save_config.assert_not_called()

    def test_configure_preserves_advanced_multi_user_auth_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            config["auth_password"] = ""
            config["base_url"] = "http://localhost:3010"
            config["secret_key"] = "ef" * 32
            config["env"].update(
                {
                    "AIOSTREAMS_AUTH": "operator-one:first-password,operator-two:second-password",
                    "AIOSTREAMS_AUTH_PERMISSIONS": "operator-one=admin,operator-two=proxy",
                }
            )
            with (
                patch.object(setup.CONFIG_MANAGER, "get", return_value=config),
                patch.object(setup.CONFIG_MANAGER, "save_config") as save_config,
                patch.object(setup, "aiostreams_runtime_ready", return_value=True),
                patch.object(
                    setup, "aiostreams_runtime_matches_selection", return_value=True
                ),
                patch.object(setup, "chown_single"),
                patch.object(setup, "chown_recursive", return_value=(True, None)),
            ):
                success, error = setup.setup_aiostreams(object(), configure_only=True)

            self.assertTrue(success, error)
            save_config.assert_not_called()
            self.assertEqual(
                "operator-one:first-password,operator-two:second-password",
                config["env"]["AIOSTREAMS_AUTH"],
            )
            self.assertEqual(
                "operator-one=admin,operator-two=proxy",
                config["env"]["AIOSTREAMS_AUTH_PERMISSIONS"],
            )

    def test_configure_accepts_loopback_subdomain_and_path_prefix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            config.update(
                {
                    "base_url": "http://aiostreams.localhost:3006/app/",
                    "secret_key": "cd" * 32,
                }
            )
            with (
                patch.object(setup.CONFIG_MANAGER, "get", return_value=config),
                patch.object(setup.CONFIG_MANAGER, "save_config") as save_config,
                patch.object(setup, "aiostreams_runtime_ready", return_value=True),
                patch.object(
                    setup, "aiostreams_runtime_matches_selection", return_value=True
                ),
                patch.object(setup, "chown_single"),
                patch.object(setup, "chown_recursive", return_value=(True, None)),
            ):
                success, error = setup.setup_aiostreams(object(), configure_only=True)

            self.assertTrue(success, error)
            save_config.assert_called_once_with("AIOStreams")
            self.assertEqual("http://aiostreams.localhost:3006/app", config["base_url"])

    def test_invalid_existing_secret_is_never_rotated(self):
        config = self._config("/aiostreams")
        config["secret_key"] = "too-short"
        with (
            patch.object(setup.CONFIG_MANAGER, "get", return_value=config),
            patch.object(setup.CONFIG_MANAGER, "save_config") as save_config,
            patch.object(setup, "aiostreams_runtime_ready", return_value=True),
            patch.object(
                setup, "aiostreams_runtime_matches_selection", return_value=True
            ),
            patch.object(setup.secrets, "token_hex") as token_hex,
        ):
            success, error = setup.setup_aiostreams(object(), configure_only=True)

        self.assertFalse(success)
        self.assertIn("64 hexadecimal", error)
        token_hex.assert_not_called()
        save_config.assert_not_called()
        self.assertEqual("too-short", config["secret_key"])

    def test_install_phase_uses_verified_oci_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._config(temp_dir)
            installed = {
                "version": "2.33.2",
                "image_digest": "sha256:" + "a" * 64,
                "oci_reference": "latest",
                "runtime_dir": str(Path(temp_dir) / "runtime"),
            }
            with (
                patch.object(setup.CONFIG_MANAGER, "get", return_value=config),
                patch.object(setup, "aiostreams_runtime_ready", return_value=False),
                patch.object(
                    setup, "aiostreams_runtime_matches_selection", return_value=False
                ),
                patch.object(
                    setup, "install_aiostreams_runtime", return_value=installed
                ) as install,
                patch.object(setup, "chown_recursive", return_value=(True, None)),
            ):
                success, error = setup.setup_aiostreams(object(), install_only=True)

            self.assertTrue(success, error)
            install.assert_called_once_with(config)


if __name__ == "__main__":
    unittest.main()
