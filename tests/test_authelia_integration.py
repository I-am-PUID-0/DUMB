import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from fastapi import HTTPException, Response
from utils.authelia_settings import managed_tpa_sso_allow_hosts

_previous_dependencies = sys.modules.get("utils.dependencies")
_dependencies = types.ModuleType("utils.dependencies")
_dependencies.get_logger = lambda: None
_dependencies.get_optional_current_user = lambda: None
_dependencies.get_updater = lambda: None
sys.modules["utils.dependencies"] = _dependencies
try:
    from api.routers.authelia import (
        LinkDumbRequest,
        LinkTPARequest,
        TPARouteRequest,
        _absolute_base_url,
        _latest_filesystem_verification_code,
        _public_origin_from_redirect_uri,
        _safe_tpa_public_routes,
        configure_tpa_route,
        discover_tpa_domains,
        latest_verification_code,
        link_dumb_auth,
        link_tpa,
    )
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


class PublicBaseUrlTests(unittest.TestCase):
    def test_public_base_url_requires_https_fqdn(self):
        self.assertEqual(
            _absolute_base_url("https://dumb.example.com", "DUMB public URL"),
            "https://dumb.example.com",
        )
        for value in (
            "http://dumb.example.com",
            "https://localhost:3005",
            "https://127.0.0.1",
            "https://dumb",
            "https://dumb.example.com/ui/dumb",
        ):
            with self.subTest(value=value):
                with self.assertRaises(HTTPException) as raised:
                    _absolute_base_url(value, "DUMB public URL")
                self.assertEqual(raised.exception.status_code, 400)
                self.assertIn("HTTPS FQDN", raised.exception.detail)

    def test_public_origin_is_derived_only_from_safe_https_redirects(self):
        self.assertEqual(
            _public_origin_from_redirect_uri(
                "https://proxy.example.com/api/auth/sso/callback"
            ),
            "https://proxy.example.com",
        )
        self.assertEqual(
            _public_origin_from_redirect_uri(
                "https://proxy.example.com:8443/api/auth/sso/callback"
            ),
            "https://proxy.example.com:8443",
        )
        for value in (
            "http://proxy.example.com/api/auth/sso/callback",
            "https://localhost/api/auth/sso/callback",
            "https://127.0.0.1/api/auth/sso/callback",
            "https://proxy/api/auth/sso/callback",
            "https://user@example.com/api/auth/sso/callback",
            "https://proxy.example.com/api/auth/sso/callback?unsafe=1",
        ):
            with self.subTest(value=value):
                self.assertEqual(_public_origin_from_redirect_uri(value), "")

    def test_public_route_discovery_is_bounded_and_sanitized(self):
        result = _safe_tpa_public_routes(
            {
                "publicRoutes": [
                    {
                        "name": "Example Service",
                        "enabled": True,
                        "targetPort": 8080,
                        "targetLoopback": True,
                        "publicUrls": [
                            "https://service.example.com",
                            "http://unsafe.example.com",
                            "https://127.0.0.1",
                        ],
                        "secretInternalField": "not returned",
                    }
                ]
            }
        )
        self.assertEqual(
            result,
            [
                {
                    "name": "Example Service",
                    "enabled": True,
                    "target_port": 8080,
                    "target_loopback": True,
                    "public_urls": ["https://service.example.com"],
                }
            ],
        )


class ManagedTPAEndpointAllowlistTests(unittest.TestCase):
    def test_returns_public_host_only_after_tpa_client_is_registered(self):
        with tempfile.TemporaryDirectory() as config_dir:
            config = {
                "config_dir": config_dir,
                "public_url": "https://auth.example.com",
            }
            self.assertEqual(managed_tpa_sso_allow_hosts(config), set())

            Path(config_dir, "dumb-managed.json").write_text(
                json.dumps({"clients": {"tpa": {"client_id": "tpa"}}}),
                encoding="utf-8",
            )

            self.assertEqual(
                managed_tpa_sso_allow_hosts(config),
                {"auth.example.com"},
            )

    def test_invalid_public_url_does_not_expand_tpa_outbound_allowlist(self):
        with tempfile.TemporaryDirectory() as config_dir:
            Path(config_dir, "dumb-managed.json").write_text(
                json.dumps({"clients": {"tpa": {"client_id": "tpa"}}}),
                encoding="utf-8",
            )

            self.assertEqual(
                managed_tpa_sso_allow_hosts(
                    {"config_dir": config_dir, "public_url": "http://localhost"}
                ),
                set(),
            )


class AutheliaVerificationCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_dir = Path(self.temp_dir.name)
        self.config = {
            "enabled": True,
            "public_url": "https://auth.example.com",
            "cookie_domain": "example.com",
            "config_dir": str(self.config_dir),
            "notifier": {"type": "filesystem"},
        }

    def _write_notifications(self):
        Path(self.config_dir, "notification.txt").write_text(
            """Date: Thu, 30 Jul 2026 12:00:00 +0000
Recipient: user@example.com
Subject: Confirm your identity

The following one-time code can be used to confirm your identity:

------------------------------
OLD12345
------------------------------

To revoke the code, visit https://auth.example.com/revoke/old

Date: Thu, 30 Jul 2026 12:01:00 +0000
Recipient: user@example.com
Subject: Confirm your identity

The following one-time code can be used to confirm your identity:

------------------------------

NEW67890

------------------------------

To revoke the code, visit https://auth.example.com/revoke/new

Date: Thu, 30 Jul 2026 12:02:00 +0000
Recipient: user@example.com
Subject: Reset your password

------------------------------
RESET999
------------------------------
""",
            encoding="utf-8",
        )

    def test_endpoint_returns_only_latest_code_with_no_store_headers(self):
        self._write_notifications()
        response = Response()
        with patch(
            "api.routers.authelia.CONFIG_MANAGER.get",
            return_value=self.config,
        ):
            result = latest_verification_code(
                response,
                _current_user="exampleadmin",
            )

        self.assertEqual(
            result,
            {
                "available": True,
                "code": "NEW67890",
                "delivery": "filesystem",
            },
        )
        self.assertEqual(response.headers["cache-control"], "no-store, private")
        self.assertNotIn("user@example.com", str(result))
        self.assertNotIn("revoke", str(result))

    def test_endpoint_requires_a_signed_in_dumb_session(self):
        response = Response()
        with self.assertRaises(HTTPException) as raised:
            latest_verification_code(response, _current_user=None)

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(
            raised.exception.headers["Cache-Control"],
            "no-store, private",
        )

    def test_endpoint_does_not_read_smtp_notifications(self):
        self.config["notifier"] = {"type": "smtp"}
        response = Response()
        with (
            patch(
                "api.routers.authelia.CONFIG_MANAGER.get",
                return_value=self.config,
            ),
            self.assertRaises(HTTPException) as raised,
        ):
            latest_verification_code(
                response,
                _current_user="exampleadmin",
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("SMTP", raised.exception.detail)

    def test_reader_rejects_a_symlinked_notification_file(self):
        external = Path(self.temp_dir.name, "other-notification.txt")
        external.write_text(
            "------------------------------\nLEAK1234\n------------------------------\n",
            encoding="utf-8",
        )
        Path(self.config_dir, "notification.txt").symlink_to(external)

        with self.assertRaises(HTTPException) as raised:
            _latest_filesystem_verification_code(self.config)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("safely", raised.exception.detail)


class AutheliaDumbLinkTests(unittest.TestCase):
    def setUp(self):
        self.authelia = {
            "enabled": True,
            "process_name": "Authelia",
            "public_url": "https://auth.example.com",
            "config_dir": "/config/authelia",
            "authorization_policy": "two_factor",
        }
        self.updater = Mock()
        self.updater.auto_update.return_value = (object(), None)
        self.logger = Mock()
        self.credentials = {
            "client_id": "dumb",
            "client_secret": "test-client-secret",
        }

    def _request(self):
        return LinkDumbRequest(
            source="managed",
            mode="hybrid",
            dumb_public_url="https://dumb.example.com",
        )

    def test_managed_client_restarts_authelia_before_enabling_dumb_oidc(self):
        with (
            patch(
                "api.routers.authelia.CONFIG_MANAGER.get",
                side_effect=lambda key: {
                    "authelia": self.authelia,
                    "postgres": {"enabled": True},
                }.get(key),
            ),
            patch(
                "api.routers.authelia.ensure_oidc_client",
                return_value=self.credentials,
            ),
            patch("api.routers.authelia.render_configuration") as render_config,
            patch.object(AUTH_CONFIG, "update_auth_provider") as update_provider,
            patch.object(AUTH_CONFIG, "enable_auth") as enable_auth,
        ):

            def restart_after_render(*_args, **_kwargs):
                render_config.assert_called_once()
                self.updater.stop_process.assert_called_once_with("Authelia")
                update_provider.assert_not_called()
                return (object(), None)

            self.updater.auto_update.side_effect = restart_after_render
            result = link_dumb_auth(
                self._request(),
                _current_user="exampleadmin",
                updater=self.updater,
                logger=self.logger,
            )

        self.updater.stop_process.assert_called_once_with("Authelia")
        self.updater.auto_update.assert_called_once_with(
            "Authelia",
            enable_update=False,
        )
        update_provider.assert_called_once()
        enable_auth.assert_called_once()
        self.assertTrue(result["linked"])

    def test_failed_authelia_restart_leaves_dumb_auth_unchanged(self):
        self.updater.auto_update.return_value = (
            None,
            "failure containing test-client-secret",
        )
        with (
            patch(
                "api.routers.authelia.CONFIG_MANAGER.get",
                side_effect=lambda key: {
                    "authelia": self.authelia,
                    "postgres": {"enabled": True},
                }.get(key),
            ),
            patch(
                "api.routers.authelia.ensure_oidc_client",
                return_value=self.credentials,
            ),
            patch("api.routers.authelia.render_configuration"),
            patch.object(AUTH_CONFIG, "update_auth_provider") as update_provider,
            patch.object(AUTH_CONFIG, "enable_auth") as enable_auth,
        ):
            with self.assertRaises(HTTPException) as raised:
                link_dumb_auth(
                    self._request(),
                    _current_user="exampleadmin",
                    updater=self.updater,
                    logger=self.logger,
                )

        self.assertEqual(raised.exception.status_code, 500)
        self.assertNotIn("test-client-secret", raised.exception.detail)
        self.updater.stop_process.assert_called_once_with("Authelia")
        update_provider.assert_not_called()
        enable_auth.assert_not_called()


class AutheliaTPALinkTests(unittest.TestCase):
    def setUp(self):
        self.authelia = {
            "enabled": True,
            "process_name": "Authelia",
            "public_url": "https://auth.example.com",
            "config_dir": "/config/authelia",
            "authorization_policy": "two_factor",
            "port": 9191,
        }
        self.tpa = {
            "enabled": True,
            "process_name": "Traefik Proxy Admin",
            "port": 3004,
            "env": {
                "DUMB_INTEGRATION_TOKEN": "t" * 48,
                "SSO_ENDPOINT_ALLOW_HOSTS": "existing.example.net",
            },
        }
        self.postgres = {"enabled": True}
        self.updater = Mock()
        self.updater.auto_update.return_value = (object(), None)
        self.logger = Mock()
        self.credentials = {
            "client_id": "traefik-proxy-admin",
            "client_secret": "test-client-secret",
        }
        self.tpa_result = {
            "linked": True,
            "providerId": "provider-example",
            "providerName": "DUMB-managed Authelia",
            "adminSsoConfigured": True,
            "localFallbackEnabled": True,
        }
        self.response = Mock()
        self.response.raise_for_status.return_value = None
        self.response.json.return_value = self.tpa_result

        def get_config(key):
            return {
                "authelia": self.authelia,
                "traefik_proxy_admin": self.tpa,
                "postgres": self.postgres,
            }.get(key)

        patchers = [
            patch(
                "api.routers.authelia.CONFIG_MANAGER.get",
                side_effect=get_config,
            ),
            patch(
                "api.routers.authelia.CONFIG_MANAGER.save_config",
                create=True,
            ),
            patch(
                "api.routers.authelia.ensure_oidc_client",
                return_value=self.credentials,
            ),
            patch("api.routers.authelia.render_configuration"),
            patch("api.routers.authelia.requests.post", return_value=self.response),
        ]
        self.mocks = [patcher.start() for patcher in patchers]
        for patcher in patchers:
            self.addCleanup(patcher.stop)
        (
            self.get_config,
            self.save_config,
            self.ensure_oidc_client,
            self.render_configuration,
            self.requests_post,
        ) = self.mocks

    def _link(self, *, restart_tpa=True):
        return link_tpa(
            LinkTPARequest(
                tpa_public_url="https://proxy.example.com",
                configure_admin_sso=True,
                allow_local_fallback=True,
                admin_groups=["admins", "operators"],
                restart_tpa=restart_tpa,
            ),
            _current_user="exampleadmin",
            updater=self.updater,
            logger=self.logger,
        )

    def test_managed_link_uses_public_https_provider_endpoints(self):
        result = self._link()

        payload = self.requests_post.call_args.kwargs["json"]
        self.assertEqual(payload["issuerUrl"], "https://auth.example.com")
        self.assertEqual(
            payload["authorizationUrl"],
            "https://auth.example.com/api/oidc/authorization",
        )
        self.assertEqual(
            payload["tokenUrl"],
            "https://auth.example.com/api/oidc/token",
        )
        self.assertEqual(
            payload["userinfoUrl"],
            "https://auth.example.com/api/oidc/userinfo",
        )
        self.assertEqual(
            payload["redirectUri"],
            "https://proxy.example.com/api/auth/sso/callback",
        )
        self.assertEqual(
            payload["tokenEndpointAuthMethod"],
            "client_secret_basic",
        )
        self.assertTrue(payload["allowLocalFallback"])
        self.assertEqual(payload["adminGroups"], ["admins", "operators"])
        self.assertTrue(result["integrationActive"])

    def test_allowlist_is_merged_deduplicated_and_saved_before_link(self):
        self.tpa["env"]["SSO_ENDPOINT_ALLOW_HOSTS"] = (
            "existing.example.net,auth.example.com,127.0.0.1," "existing.example.net"
        )

        def assert_environment_was_persisted(*_args, **_kwargs):
            self.assertTrue(self.save_config.called)
            self.assertEqual(
                self.tpa["env"]["SSO_ENDPOINT_ALLOW_HOSTS"],
                "127.0.0.1,auth.example.com,existing.example.net",
            )
            return self.response

        self.requests_post.side_effect = assert_environment_was_persisted

        self._link()
        self._link()

        entries = self.tpa["env"]["SSO_ENDPOINT_ALLOW_HOSTS"].split(",")
        self.assertEqual(entries.count("127.0.0.1"), 1)
        self.assertEqual(entries.count("auth.example.com"), 1)
        self.assertEqual(entries.count("existing.example.net"), 1)

    def test_declined_restart_reports_persisted_but_not_active_environment(self):
        result = self._link(restart_tpa=False)

        self.updater.stop_process.assert_called_once_with("Authelia")
        self.updater.auto_update.assert_called_once_with(
            "Authelia",
            enable_update=False,
        )
        self.assertTrue(result["environmentPersisted"])
        self.assertFalse(result["restartCompleted"])
        self.assertTrue(result["restartRequired"])
        self.assertFalse(result["integrationActive"])

    def test_requested_restart_happens_after_environment_is_persisted(self):
        def assert_saved_before_restart(process_name, **_kwargs):
            if process_name == "Traefik Proxy Admin":
                self.assertTrue(self.save_config.called)
            return (object(), None)

        self.updater.auto_update.side_effect = assert_saved_before_restart

        result = self._link(restart_tpa=True)

        self.assertEqual(
            self.updater.auto_update.call_args_list,
            [
                call("Authelia", enable_update=False),
                call(
                    "Traefik Proxy Admin",
                    enable_update=False,
                ),
            ],
        )
        self.assertEqual(
            self.updater.stop_process.call_args_list,
            [call("Authelia"), call("Traefik Proxy Admin")],
        )
        self.assertTrue(result["restartCompleted"])
        self.assertFalse(result["restartRequired"])
        self.assertTrue(result["integrationActive"])

    def test_new_integration_token_keeps_required_restart_behavior(self):
        self.tpa["env"].pop("DUMB_INTEGRATION_TOKEN")

        def assert_restarted_before_link(*_args, **_kwargs):
            self.assertEqual(self.updater.auto_update.call_count, 2)
            return self.response

        self.requests_post.side_effect = assert_restarted_before_link

        result = self._link(restart_tpa=False)

        self.assertEqual(
            self.updater.stop_process.call_args_list,
            [call("Authelia"), call("Traefik Proxy Admin")],
        )
        self.assertTrue(result["restartCompleted"])
        self.assertFalse(result["restartRequired"])
        self.assertTrue(result["integrationActive"])

    def test_response_and_logs_do_not_expose_link_secrets(self):
        self.response.json.return_value = {
            **self.tpa_result,
            "clientSecret": self.credentials["client_secret"],
            "integrationToken": self.tpa["env"]["DUMB_INTEGRATION_TOKEN"],
            "unexpected": "not part of the response contract",
        }

        result = self._link()

        serialized_result = repr(result)
        serialized_logs = repr(self.logger.method_calls)
        self.assertNotIn(self.credentials["client_secret"], serialized_result)
        self.assertNotIn(
            self.tpa["env"]["DUMB_INTEGRATION_TOKEN"],
            serialized_result,
        )
        self.assertNotIn("clientSecret", result)
        self.assertNotIn("integrationToken", result)
        self.assertNotIn("unexpected", result)
        self.assertNotIn(self.credentials["client_secret"], serialized_logs)
        self.assertNotIn(
            self.tpa["env"]["DUMB_INTEGRATION_TOKEN"],
            serialized_logs,
        )

    def test_restart_failure_does_not_return_or_log_updater_details(self):
        failure_detail = "failure containing test-client-secret and " + ("t" * 48)
        self.updater.auto_update.return_value = (None, failure_detail)

        with self.assertRaises(HTTPException) as raised:
            self._link()

        self.assertEqual(raised.exception.status_code, 500)
        self.assertNotIn(failure_detail, str(raised.exception.detail))
        self.assertNotIn(failure_detail, repr(self.logger.method_calls))

    def test_external_provider_endpoints_are_not_rewritten(self):
        for source in ("external_authelia", "custom_oidc"):
            with self.subTest(source=source):
                request = OIDCProviderRequest(
                    mode="hybrid",
                    source=source,
                    issuer_url="https://issuer.example.com",
                    authorization_endpoint=(
                        "https://browser.example.net/oauth/authorize"
                    ),
                    token_endpoint="https://token.example.net/oauth/token",
                    userinfo_endpoint="https://userinfo.example.net/oauth/userinfo",
                    client_id="dumb",
                    client_secret="test-client-secret",
                    redirect_uri="https://dumb.example.com/api/auth/oidc/callback",
                )
                with patch.object(
                    AUTH_CONFIG,
                    "get_oidc_config",
                    return_value={"source": source},
                ):
                    validated = _validate_oidc_provider(request)["oidc"]

                self.assertEqual(
                    validated["authorization_endpoint"],
                    "https://browser.example.net/oauth/authorize",
                )
                self.assertEqual(
                    validated["token_endpoint"],
                    "https://token.example.net/oauth/token",
                )
                self.assertEqual(
                    validated["userinfo_endpoint"],
                    "https://userinfo.example.net/oauth/userinfo",
                )


class AutheliaTPARouteProxyTests(unittest.TestCase):
    def setUp(self):
        self.logger = Mock()
        self.response = Mock()
        self.response.ok = True
        self.response.status_code = 200

    def test_domain_discovery_returns_only_safe_normalized_fields(self):
        self.response.json.return_value = {
            "domains": [
                {
                    "id": "domain-id",
                    "name": "Example",
                    "domain": "EXAMPLE.COM",
                    "isDefault": True,
                    "useWildcardCert": True,
                    "certResolver": "",
                    "serviceCount": 4,
                    "unexpected": "not returned",
                }
            ],
            "routeApplications": ["authelia", "dumb", "tpa", "unexpected"],
            "publicRoutes": [],
        }
        with (
            patch(
                "api.routers.authelia._tpa_integration_context",
                return_value=({}, "t" * 48, "http://127.0.0.1:3004"),
            ),
            patch(
                "api.routers.authelia.requests.get",
                return_value=self.response,
            ) as request_get,
        ):
            result = discover_tpa_domains(
                _current_user="exampleadmin",
                logger=self.logger,
            )

        self.assertEqual(
            result,
            {
                "domains": [
                    {
                        "id": "domain-id",
                        "name": "Example",
                        "domain": "example.com",
                        "is_default": True,
                        "use_wildcard_cert": True,
                        "cert_resolver": "",
                        "service_count": 4,
                    }
                ],
                "route_applications": ["authelia", "dumb", "tpa"],
                "public_routes": [],
            },
        )
        request_get.assert_called_once_with(
            "http://127.0.0.1:3004/api/integrations/dumb/authelia/route",
            headers={"Authorization": f"Bearer {'t' * 48}"},
            timeout=10,
        )

    def test_route_configuration_uses_managed_url_and_port(self):
        self.response.json.return_value = {
            "configured": True,
            "created": True,
            "reused": False,
            "serviceId": "service-id",
            "hostname": "auth.example.com",
            "domain": {"domain": "example.com"},
            "targetHost": "127.0.0.1",
            "targetPort": 9191,
            "targetHttps": False,
            "authentication": "none",
        }
        with (
            patch(
                "api.routers.authelia.CONFIG_MANAGER.get",
                return_value={
                    "public_url": "https://auth.example.com",
                    "port": 9191,
                },
            ),
            patch(
                "api.routers.authelia._tpa_integration_context",
                return_value=({}, "t" * 48, "http://127.0.0.1:3004"),
            ),
            patch(
                "api.routers.authelia.requests.post",
                return_value=self.response,
            ) as request_post,
        ):
            result = configure_tpa_route(
                TPARouteRequest(domain_id="domain-id"),
                _current_user="exampleadmin",
                logger=self.logger,
            )

        self.assertTrue(result["configured"])
        self.assertTrue(result["created"])
        request_post.assert_called_once_with(
            "http://127.0.0.1:3004/api/integrations/dumb/authelia/route",
            json={
                "domainId": "domain-id",
                "publicUrl": "https://auth.example.com",
                "targetHost": "127.0.0.1",
                "targetPort": 9191,
                "application": "authelia",
            },
            headers={"Authorization": f"Bearer {'t' * 48}"},
            timeout=20,
        )

    def test_dumb_and_tpa_routes_use_managed_loopback_ports(self):
        for application, public_url, expected_port in (
            ("dumb", "https://dumb.example.com", 3005),
            ("tpa", "https://proxy.example.com", 3004),
        ):
            with self.subTest(application=application):
                self.response.json.return_value = {
                    "application": application,
                    "configured": True,
                    "created": True,
                    "hostname": public_url.removeprefix("https://"),
                    "targetPort": expected_port,
                }

                def get_config(key):
                    if key == "authelia":
                        return {"public_url": "https://auth.example.com"}
                    if key == "dumb":
                        return {"frontend": {"enabled": True, "port": 3005}}
                    return None

                with (
                    patch(
                        "api.routers.authelia.CONFIG_MANAGER.get",
                        side_effect=get_config,
                    ),
                    patch(
                        "api.routers.authelia._tpa_integration_context",
                        return_value=(
                            {"enabled": True, "port": 3004},
                            "t" * 48,
                            "http://127.0.0.1:3004",
                        ),
                    ),
                    patch(
                        "api.routers.authelia.requests.post",
                        return_value=self.response,
                    ) as request_post,
                ):
                    result = configure_tpa_route(
                        TPARouteRequest(
                            domain_id="domain-id",
                            application=application,
                            public_url=public_url,
                        ),
                        _current_user="exampleadmin",
                        logger=self.logger,
                    )

                self.assertEqual(result["application"], application)
                request_post.assert_called_once_with(
                    "http://127.0.0.1:3004/api/integrations/dumb/authelia/route",
                    json={
                        "domainId": "domain-id",
                        "publicUrl": public_url,
                        "targetHost": "127.0.0.1",
                        "targetPort": expected_port,
                        "application": application,
                    },
                    headers={"Authorization": f"Bearer {'t' * 48}"},
                    timeout=20,
                )

    def test_external_dev_frontend_can_supply_a_traefik_reachable_target(self):
        self.response.json.return_value = {
            "application": "dumb",
            "configured": True,
            "created": True,
            "hostname": "dumb.example.com",
            "targetHost": "dmbdb_dev",
            "targetPort": 3005,
        }

        def get_config(key):
            if key == "authelia":
                return {"public_url": "https://auth.example.com"}
            if key == "dumb":
                return {"frontend": {"enabled": False, "port": 3005}}
            return None

        with (
            patch(
                "api.routers.authelia.CONFIG_MANAGER.get",
                side_effect=get_config,
            ),
            patch(
                "api.routers.authelia._tpa_integration_context",
                return_value=(
                    {"enabled": True, "port": 3004},
                    "t" * 48,
                    "http://127.0.0.1:3004",
                ),
            ),
            patch(
                "api.routers.authelia.requests.post",
                return_value=self.response,
            ) as request_post,
        ):
            result = configure_tpa_route(
                TPARouteRequest(
                    domain_id="domain-id",
                    application="dumb",
                    public_url="https://dumb.example.com",
                    target_host="dmbdb_dev",
                    target_port=3005,
                ),
                _current_user="exampleadmin",
                logger=self.logger,
            )

        self.assertTrue(result["configured"])
        request_post.assert_called_once_with(
            "http://127.0.0.1:3004/api/integrations/dumb/authelia/route",
            json={
                "domainId": "domain-id",
                "publicUrl": "https://dumb.example.com",
                "targetHost": "dmbdb_dev",
                "targetPort": 3005,
                "application": "dumb",
            },
            headers={"Authorization": f"Bearer {'t' * 48}"},
            timeout=20,
        )

    def test_external_dev_frontend_requires_a_safe_target_host(self):
        def get_config(key):
            if key == "authelia":
                return {"public_url": "https://auth.example.com"}
            if key == "dumb":
                return {"frontend": {"enabled": False}}
            return None

        with (
            patch(
                "api.routers.authelia.CONFIG_MANAGER.get",
                side_effect=get_config,
            ),
            patch(
                "api.routers.authelia._tpa_integration_context",
                return_value=(
                    {"enabled": True, "port": 3004},
                    "t" * 48,
                    "http://127.0.0.1:3004",
                ),
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                configure_tpa_route(
                    TPARouteRequest(
                        domain_id="domain-id",
                        application="dumb",
                        public_url="https://dumb.example.com",
                        target_host="http://dmbdb_dev/path",
                    ),
                    _current_user="exampleadmin",
                    logger=self.logger,
                )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("target host", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
