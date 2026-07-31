import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi import HTTPException

_previous_dependencies = sys.modules.get("utils.dependencies")
_dependencies = types.ModuleType("utils.dependencies")
_dependencies.get_logger = lambda: None
_dependencies.get_optional_current_user = lambda: None
sys.modules["utils.dependencies"] = _dependencies
try:
    from api.routers.auth import (
        AUTH_CONFIG,
        OIDCProviderRequest,
        _validate_oidc_provider,
    )
finally:
    if _previous_dependencies is None:
        sys.modules.pop("utils.dependencies", None)
    else:
        sys.modules["utils.dependencies"] = _previous_dependencies

from utils.auth_config import AuthConfigManager
from utils.authelia_settings import (
    authelia_environment,
    bootstrap_user,
    ensure_oidc_client,
    render_configuration,
)


class AuthProviderConfigTests(unittest.TestCase):
    def test_default_config_path_can_be_overridden_for_isolated_runtimes(self):
        with tempfile.TemporaryDirectory() as directory:
            configured_path = Path(directory, "auth", "users.json")
            with patch.dict(
                os.environ,
                {"DUMB_AUTH_CONFIG_PATH": str(configured_path)},
            ):
                manager = AuthConfigManager()

            self.assertEqual(manager.config_path, str(configured_path))
            self.assertTrue(configured_path.is_file())

    def test_explicit_config_path_wins_over_environment_override(self):
        with tempfile.TemporaryDirectory() as directory:
            explicit_path = Path(directory, "explicit", "users.json")
            environment_path = Path(directory, "environment", "users.json")
            with patch.dict(
                os.environ,
                {"DUMB_AUTH_CONFIG_PATH": str(environment_path)},
            ):
                manager = AuthConfigManager(str(explicit_path))

            self.assertEqual(manager.config_path, str(explicit_path))
            self.assertTrue(explicit_path.is_file())
            self.assertFalse(environment_path.exists())

    def test_legacy_config_defaults_to_local_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "users.json")
            path.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "users": [],
                        "jwt_secret": "test-secret",
                    }
                ),
                encoding="utf-8",
            )

            manager = AuthConfigManager(str(path))

            self.assertEqual(manager.get_auth_mode(), "local")
            self.assertTrue(manager.local_login_enabled())
            self.assertFalse(manager.oidc_login_enabled())

    def test_oidc_group_authorization_uses_token_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AuthConfigManager(str(Path(directory, "users.json")))
            manager.update_auth_provider(
                "hybrid",
                {
                    "enabled": True,
                    "allowed_groups": ["DUMB Admins"],
                },
            )

            allowed = types.SimpleNamespace(
                sub="alice", provider="oidc", groups=["dumb admins"]
            )
            denied = types.SimpleNamespace(
                sub="bob", provider="oidc", groups=["viewers"]
            )
            self.assertTrue(manager.validate_token_principal(allowed))
            self.assertFalse(manager.validate_token_principal(denied))

    def test_switching_provider_source_requires_a_new_client_secret(self):
        request = OIDCProviderRequest(
            mode="hybrid",
            source="custom_oidc",
            issuer_url="https://sso.example.com",
            client_id="dumb",
            redirect_uri="https://dumb.example.com/api/auth/oidc/callback",
        )
        with patch.object(
            AUTH_CONFIG,
            "get_oidc_config",
            return_value={
                "source": "managed",
                "client_secret": "existing-managed-secret",
            },
        ):
            with self.assertRaises(HTTPException) as raised:
                _validate_oidc_provider(request)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("client_secret", raised.exception.detail)

    def test_redirect_uri_requires_browser_facing_https_fqdn(self):
        for redirect_uri in (
            "http://localhost:3005/api/auth/oidc/callback",
            "https://127.0.0.1/api/auth/oidc/callback",
            "https://dumb/api/auth/oidc/callback",
            "https://dumb.example.com/api/auth/oidc/callback?next=/",
        ):
            with self.subTest(redirect_uri=redirect_uri):
                request = OIDCProviderRequest(
                    mode="hybrid",
                    source="custom_oidc",
                    issuer_url="https://sso.example.com",
                    client_id="dumb",
                    client_secret="test-client-secret",
                    redirect_uri=redirect_uri,
                )
                with patch.object(
                    AUTH_CONFIG,
                    "get_oidc_config",
                    return_value={"source": "custom_oidc"},
                ):
                    with self.assertRaises(HTTPException) as raised:
                        _validate_oidc_provider(request)

                self.assertEqual(raised.exception.status_code, 400)
                self.assertIn("HTTPS FQDN", raised.exception.detail)


class ManagedAutheliaConfigTests(unittest.TestCase):
    def test_initial_environment_does_not_enable_oidc_without_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = authelia_environment({"config_dir": directory})

            self.assertNotIn(
                "AUTHELIA_IDENTITY_PROVIDERS_OIDC_HMAC_SECRET_FILE",
                environment,
            )

    def test_initial_config_omits_oidc_until_a_client_is_registered(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "utils.authelia_settings._private_jwk",
                return_value="test-private-jwk",
            ) as private_jwk:
                config_path = render_configuration(
                    {
                        "config_dir": directory,
                        "public_url": "https://auth.example.com",
                        "cookie_domain": "example.com",
                        "authorization_policy": "two_factor",
                        "notifier": {"type": "filesystem"},
                        "log_file": str(Path(directory, "authelia.log")),
                    },
                    {
                        "host": "127.0.0.1",
                        "port": 5432,
                        "user": "DUMB",
                        "password": "database-secret",
                    },
                )

            config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
            self.assertNotIn("identity_providers", config)
            private_jwk.assert_not_called()

    def test_rendered_config_uses_hashed_users_and_separate_client_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            bootstrap_user(
                directory,
                username="exampleadmin",
                password="example-authelia-password",
                display_name="Example Admin",
                email="admin@example.com",
                groups=["admins"],
            )
            client = ensure_oidc_client(
                directory,
                key="dumb",
                client_id="dumb",
                client_name="DUMB",
                redirect_uri="https://dumb.example.com/api/auth/oidc/callback",
            )
            with patch(
                "utils.authelia_settings._private_jwk",
                return_value="test-private-jwk",
            ):
                config_path = render_configuration(
                    {
                        "config_dir": directory,
                        "public_url": "https://auth.example.com",
                        "cookie_domain": "example.com",
                        "authorization_policy": "two_factor",
                        "notifier": {"type": "filesystem"},
                        "log_file": str(Path(directory, "authelia.log")),
                    },
                    {
                        "host": "127.0.0.1",
                        "port": 5432,
                        "user": "DUMB",
                        "password": "database-secret",
                    },
                )

            users_text = Path(directory, "users_database.yml").read_text(
                encoding="utf-8"
            )
            config_text = Path(config_path).read_text(encoding="utf-8")
            config = yaml.safe_load(config_text)
            self.assertNotIn("example-authelia-password", users_text)
            self.assertNotIn(client["client_secret"], config_text)
            self.assertNotIn("database-secret", config_text)
            self.assertEqual(
                config["identity_providers"]["oidc"]["clients"][0]["client_id"],
                "dumb",
            )
            self.assertEqual(os.stat(config_path).st_mode & 0o777, 0o600)

    def test_oidc_environment_is_enabled_after_client_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            ensure_oidc_client(
                directory,
                key="dumb",
                client_id="dumb",
                client_name="DUMB",
                redirect_uri="https://dumb.example.com/api/auth/oidc/callback",
            )

            environment = authelia_environment({"config_dir": directory})

            self.assertEqual(
                environment["AUTHELIA_IDENTITY_PROVIDERS_OIDC_HMAC_SECRET_FILE"],
                str(Path(directory, "secrets", "oidc-hmac")),
            )


if __name__ == "__main__":
    unittest.main()
