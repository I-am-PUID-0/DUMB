"""Version-shaped account bootstrap and handshake fixtures; no live NNTP."""

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from utils import arrangement_providers as providers
from utils.arrangement_http import ApiError, ApiResponse


class FakeProviderClient:
    def __init__(self, service):
        self.service = service
        self.calls = []
        self.config = {
            "providers": [],
            "import": {
                "import_strategy": "NONE",
                "import_dir": "/library/old",
                "watch_dir": "/watch",
                "user_setting": True,
            },
        }
        self.infini = {
            "Providers": [],
            "ProviderGeneration": 3,
            "KeepFutureField": True,
        }
        self.decypharr = {
            "usenet": {"providers": [], "disk_path": "/cache/usenet"},
            "mount": {"type": "dfs", "mount_path": "/mnt/debrid"},
            "arrs": [
                {
                    "name": "preserve",
                    "host": "http://127.0.0.1:7878",
                    "token": "private-arr-fixture",
                }
            ],
            "future_setting": True,
        }
        self.authenticate = True
        self.api_status = None
        self.readback_wrong = False
        self.cookies = {}
        self.login_admin = True
        self.login_cookie = True
        self.decypharr_stale_read = False
        self.old_decypharr = None
        self.reject_settings_session = False

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, copy.deepcopy(kwargs)))
        if self.api_status is not None:
            return ApiResponse(self.api_status, b'{"error":"private-upstream-secret"}')
        if path == "/api/auth/login":
            if self.login_cookie:
                self.cookies["JWT"] = "eyJhbGciOiJIUzI1NiJ9.fixture.signature"
            return self.response(
                {"success": True, "data": {"user": {"is_admin": self.login_admin}}}
            )
        if path == "/api/get-config":
            value = copy.deepcopy(self.infini)
            for item in value["Providers"]:
                item["Pass"] = "__NZBDAV_SECRET_MASK_V1__:fixture-mask"
            return self.response(
                {
                    "configItems": [
                        {
                            "configName": "usenet.providers",
                            "configValue": json.dumps(value),
                        }
                    ]
                }
            )
        if path == "/api/update-config":
            self.infini = json.loads(kwargs["data"]["usenet.providers"])
            return self.response({"status": True})
        if path == "/api/test-usenet-connection":
            return self.response({"status": True, "connected": self.authenticate})
        if path == "/api/providers/test":
            return self.response(
                {"success": True, "data": {"success": self.authenticate}}
            )
        if path == "/api/config/import":
            self.config["import"].update(kwargs["json"]["import"])
            return self.response({"success": True, "data": self.config})
        if path.startswith("/api/providers") and method in {"POST", "PUT"}:
            body = copy.deepcopy(kwargs["json"])
            if method == "POST":
                body.update(id="provider_1", password_set=True)
                self.config["providers"].append(body)
            else:
                self.config["providers"][0].update(body)
                body = self.config["providers"][0]
            dto = {key: value for key, value in body.items() if key != "password"}
            return self.response({"success": True, "data": dto})
        if path == "/api/config":
            if self.reject_settings_session:
                return ApiResponse(401, b'{"message":"Authentication required"}')
            if self.service == "decypharr":
                if method == "POST":
                    self.old_decypharr = copy.deepcopy(self.decypharr)
                    self.decypharr = copy.deepcopy(kwargs["json"])
                    return self.response({"status": "success", "restarted": True})
                value = copy.deepcopy(self.decypharr)
                if self.decypharr_stale_read and self.old_decypharr is not None:
                    value = copy.deepcopy(self.old_decypharr)
                    self.decypharr_stale_read = False
                for item in (value.get("usenet") or {}).get("providers", []):
                    if item.get("ssl") is False:
                        item.pop("ssl")
                return self.response(value)
            value = copy.deepcopy(self.config)
            for item in value["providers"]:
                item.pop("password", None)
            if self.readback_wrong and value["providers"]:
                value["providers"][0]["max_connections"] += 1
            return self.response({"success": True, "data": value})
        raise AssertionError((method, path))

    @staticmethod
    def response(value):
        return ApiResponse(200, json.dumps(value).encode())


class ProviderAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.account = {
            "name": "Primary",
            "host": "news.example.invalid",
            "port": 563,
            "username": "fixture-user",
            "password": "private-account-fixture",
            "tls": True,
            "connections": 5,
            "account_connection_limit": 10,
        }
        self.enterContext(
            mock.patch.object(
                providers,
                "validate_endpoint",
                return_value=(None, 563, ["203.0.113.1"]),
            )
        )

    def fixture(self, service):
        directory = self.root / service
        directory.mkdir(exist_ok=True)
        (directory / "version.txt").write_text("v2.5")
        (directory / "config.json").write_text('{"url_base": "/native-prefix"}')
        (directory / "auth.json").write_text('{"api_token": "private-api-fixture"}')
        config = {
            service: {
                "enabled": True,
                "config_dir": str(directory),
                "config_file": str(directory / "config.json"),
                "port": 8088,
                "backend_port": 8080,
                "env": {
                    "ALTMOUNT_API_KEY": "private-api-fixture",
                    "FRONTEND_BACKEND_API_KEY": "private-api-fixture",
                },
            }
        }
        action = {
            "source": service,
            "service_key": service,
            "action": "nntp",
            "id": service + "::nntp",
        }
        payload = {
            "arrangement": {
                "values": {service + "::nntp": [copy.deepcopy(self.account)]}
            }
        }
        client = FakeProviderClient(service)
        factory = mock.Mock(return_value=client)
        return config, action, payload, client, factory

    def invoke(self, fixture, *, inspect=False, authenticator=None):
        config, action, payload, _client, factory = fixture
        fn = providers.inspect_provider if inspect else providers.apply_provider
        return fn(
            action,
            config,
            payload,
            client_factory=factory,
            nntp_authenticator=authenticator or mock.Mock(return_value=True),
        )

    def test_all_three_provider_bootstraps_preserve_config_and_verify_no_content(self):
        for service in ("infinidysk", "altmount", "decypharr"):
            with self.subTest(service=service):
                fixture = self.fixture(service)
                result = self.invoke(fixture)
                self.assertEqual(result["execution"], "applied")
                self.assertEqual(result["verification"], "passed")
                self.assertFalse(result["evidence"]["content_tested"])
                self.assertNotIn("private-account", json.dumps(result))
                self.assertNotIn("fixture-user", json.dumps(result))
                path = (
                    Path(fixture[0][service]["config_dir"])
                    / ".dumb-nntp-ownership.json"
                )
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertNotIn("private-account", path.read_text())
                self.assertEqual(self.invoke(fixture)["execution"], "already_present")
                client = fixture[3]
                self.assertFalse(
                    any(
                        "speedtest" in call[1] or "download" in call[1]
                        for call in client.calls
                    )
                )
                if service == "decypharr":
                    self.assertTrue(client.decypharr["future_setting"])
                    self.assertEqual(client.decypharr["mount"]["type"], "dfs")
                    self.assertEqual(
                        client.decypharr["usenet"]["disk_path"], "/cache/usenet"
                    )
                if service == "infinidysk":
                    self.assertTrue(client.infini["KeepFutureField"])
                if service == "altmount":
                    self.assertTrue(client.config["import"]["user_setting"])

    def test_inspection_of_unapplied_plan_never_mutates_or_authenticates(self):
        for service in providers.PROVIDERS:
            fixture = self.fixture(service)
            result = self.invoke(fixture, inspect=True)
            self.assertEqual(result["execution"], "planned")
            self.assertFalse(
                any(
                    call[1].endswith("/test")
                    or "update-config" in call[1]
                    or call[0] in {"PUT", "PATCH"}
                    for call in fixture[3].calls
                )
            )
            self.assertFalse(
                (
                    Path(fixture[0][service]["config_dir"])
                    / ".dumb-nntp-ownership.json"
                ).exists()
            )

    def test_failed_account_authentication_changes_no_application_settings(self):
        for service in providers.PROVIDERS:
            fixture = self.fixture(service)
            fixture[3].authenticate = False
            result = self.invoke(fixture, authenticator=mock.Mock(return_value=False))
            self.assertEqual(result["execution"], "waiting_input")
            self.assertEqual(result["verification"], "failed")
            self.assertFalse(
                any(
                    call[1] in {"/api/providers", "/api/update-config"}
                    or (call[0] == "POST" and call[1] == "/api/config")
                    for call in fixture[3].calls
                )
            )

    def test_unowned_existing_account_is_preserved_even_if_same_endpoint(self):
        fixture = self.fixture("altmount")
        existing = {
            "id": "user-owned",
            "host": self.account["host"],
            "port": 563,
            "username": self.account["username"],
            "tls": True,
            "max_connections": 20,
            "enabled": True,
            "quota_bytes": 1000,
        }
        fixture[3].config["providers"] = [existing]
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertEqual(fixture[3].config["providers"], [existing])
        self.assertFalse(any(call[0] == "PUT" for call in fixture[3].calls))

    def test_owned_account_rotation_preserves_policies_and_detects_external_edits(self):
        fixture = self.fixture("altmount")
        self.invoke(fixture)
        fixture[3].config["providers"][0].update(
            quota_bytes=1000, is_backup_provider=True
        )
        fixture[2]["arrangement"]["values"]["altmount::nntp"][0].update(
            password="rotated-fixture", connections=6
        )
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "applied")
        self.assertEqual(fixture[3].config["providers"][0]["quota_bytes"], 1000)
        self.assertTrue(fixture[3].config["providers"][0]["is_backup_provider"])
        fixture[3].config["providers"][0]["max_connections"] = 8
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")

    def test_intentional_disable_is_not_reenabled_or_reported_verified(self):
        fixture = self.fixture("altmount")
        self.invoke(fixture)
        fixture[3].config["providers"][0]["enabled"] = False
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")
        self.assertFalse(fixture[3].config["providers"][0]["enabled"])

    def test_infini_preserves_masked_passwords_and_provider_budget_metadata(self):
        fixture = self.fixture("infinidysk")
        fixture[3].infini["Providers"] = [
            {
                "ProviderId": "unowned-other",
                "Host": "other.example.invalid",
                "Port": 563,
                "User": "other",
                "Pass": "other-private",
                "UseSsl": True,
                "MaxConnections": 5,
                "Type": 1,
                "ByteLimit": 100000,
            }
        ]
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "applied")
        update = next(
            call[2]["data"]
            for call in fixture[3].calls
            if call[1] == "/api/update-config"
        )
        account = json.loads(update["usenet.providers"])["Providers"][0]
        self.assertEqual(account["Pass"], "__NZBDAV_SECRET_MASK_V1__:fixture-mask")
        self.assertEqual(account["ByteLimit"], 100000)

    def altmount_administrator_fixture(self):
        fixture = self.fixture("altmount")
        fixture[2]["arrangement"]["values"].update(
            {
                "altmount::admin_username": "fixture-admin",
                "altmount::admin_password": "private-admin-password",
            }
        )
        return fixture

    def test_altmount_explicit_admin_login_exchanges_temporary_password_for_reusable_settings_session(
        self,
    ):
        fixture = self.altmount_administrator_fixture()
        preview = self.invoke(fixture, inspect=True)
        self.assertEqual(preview["execution"], "waiting_input")
        self.assertFalse(fixture[3].calls)
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "applied")
        session = result["_private_values"]["altmount::settings_token"]
        self.assertEqual(result["_clear_inputs"], ["altmount::admin_password"])
        public = {
            key: value for key, value in result.items() if not key.startswith("_")
        }
        self.assertNotIn(session, json.dumps(public))
        values = fixture[2]["arrangement"]["values"]
        values.update(result["_private_values"])
        values.pop("altmount::admin_password")
        fixture[3].calls.clear()
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "already_present")
        self.assertFalse(any(call[1] == "/api/auth/login" for call in fixture[3].calls))
        self.assertEqual(
            fixture[4].call_args.kwargs["headers"]["Cookie"], "JWT=" + session
        )

    def test_altmount_successful_session_handoff_is_not_lost_when_nntp_validation_fails(
        self,
    ):
        fixture = self.altmount_administrator_fixture()
        fixture[3].authenticate = False
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertTrue(result["_private_values"]["altmount::settings_token"])
        self.assertEqual(result["_clear_inputs"], ["altmount::admin_password"])
        self.assertFalse(any(call[1] == "/api/providers" for call in fixture[3].calls))

    def test_altmount_session_must_authorize_settings_before_password_is_cleared(self):
        fixture = self.altmount_administrator_fixture()
        fixture[3].reject_settings_session = True
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertNotIn("_private_values", result)
        self.assertNotIn("_clear_inputs", result)
        self.assertFalse(any(call[1] == "/api/providers" for call in fixture[3].calls))

    def test_altmount_expired_session_requests_fresh_authorization_without_automatic_login(
        self,
    ):
        fixture = self.fixture("altmount")
        fixture[2]["arrangement"]["values"].update(
            {
                "altmount::admin_username": "fixture-admin",
                "altmount::settings_token": "eyJhbGciOiJIUzI1NiJ9.expired.signature",
            }
        )
        fixture[3].api_status = 401
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertFalse(result["retryable"])
        self.assertIn("administrator settings session", result["reason"])
        self.assertFalse(any(call[1] == "/api/auth/login" for call in fixture[3].calls))
        self.assertNotIn("_private_values", result)

    def test_altmount_nonadmin_and_missing_cookie_responses_never_authorize_settings(
        self,
    ):
        for property_name, expected in (
            ("login_admin", "waiting_input"),
            ("login_cookie", "unsupported"),
        ):
            with self.subTest(property_name=property_name):
                fixture = self.altmount_administrator_fixture()
                setattr(fixture[3], property_name, False)
                result = self.invoke(fixture)
                self.assertEqual(result["execution"], expected)
                self.assertNotIn("_private_values", result)
                self.assertFalse(
                    any(
                        call[1] in {"/api/config", "/api/providers"}
                        for call in fixture[3].calls
                    )
                )

    def test_decypharr_async_restart_recheck_reuses_saved_provider_without_duplicate_write(
        self,
    ):
        fixture = self.fixture("decypharr")
        fixture[3].decypharr_stale_read = True
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "waiting_dependency")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["verification"], "not_checked")
        fixture[3].calls.clear()
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "already_present")
        self.assertEqual(result["verification"], "passed")
        self.assertFalse(
            any(
                call[0] == "POST" and call[1] == "/api/config"
                for call in fixture[3].calls
            )
        )

    def test_decypharr_manual_provider_password_edits_are_preserved(self):
        fixture = self.fixture("decypharr")
        self.assertEqual(self.invoke(fixture)["execution"], "applied")
        fixture[3].decypharr["usenet"]["providers"][0][
            "password"
        ] = "private-user-replacement"
        fixture[3].calls.clear()
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertFalse(any(call[0] == "POST" for call in fixture[3].calls))
        self.assertEqual(
            fixture[3].decypharr["usenet"]["providers"][0]["password"],
            "private-user-replacement",
        )

    def test_fresh_decypharr_url_base_middleware_block_is_actionable_manual_input(self):
        fixture = self.fixture("decypharr")
        fixture[3].api_status = 503
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertFalse(result["retryable"])
        self.assertIn("URL base", result["reason"])
        self.assertFalse(any(call[0] == "POST" for call in fixture[3].calls))

    def test_decypharr_native_omitted_false_ssl_is_verified_and_idempotent(self):
        fixture = self.fixture("decypharr")
        fixture[2]["arrangement"]["values"]["decypharr::nntp"][0]["tls"] = False
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "applied")
        self.assertEqual(result["verification"], "passed")
        fixture[3].calls.clear()
        self.assertEqual(self.invoke(fixture)["execution"], "already_present")
        self.assertFalse(
            any(
                call[0] == "POST" and call[1] == "/api/config"
                for call in fixture[3].calls
            )
        )

    def test_fresh_decypharr_v2_with_omitted_empty_section_can_be_bootstrapped(self):
        fixture = self.fixture("decypharr")
        fixture[3].decypharr.pop("usenet")
        self.assertEqual(self.invoke(fixture)["execution"], "applied")

    def test_older_decypharr_schema_and_unknown_provider_schema_are_explicit(self):
        fixture = self.fixture("decypharr")
        fixture[3].decypharr.pop("usenet")
        Path(fixture[0]["decypharr"]["config_dir"], "version.txt").write_text("v1.1.6")
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "unsupported")
        self.assertEqual(result["verification"], "unavailable")

    def test_api_rejection_and_unavailability_are_separate_and_redacted(self):
        for status, execution in (
            (401, "waiting_input"),
            (403, "waiting_input"),
            (404, "unsupported"),
            (503, "waiting_dependency"),
        ):
            with self.subTest(status=status):
                fixture = self.fixture("altmount")
                fixture[3].api_status = status
                result = self.invoke(fixture)
                self.assertEqual(result["execution"], execution)
                self.assertNotIn("private-upstream-secret", json.dumps(result))

    def test_provider_readback_mismatch_cannot_be_reported_as_success(self):
        fixture = self.fixture("altmount")
        fixture[3].readback_wrong = True
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(result["verification"], "failed")

    def test_missing_credentials_require_manual_authorization(self):
        fixture = self.fixture("altmount")
        fixture[0]["altmount"]["env"] = {}
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "waiting_input")

    def test_decypharr_native_base_is_preserved_for_api_connections(self):
        fixture = self.fixture("decypharr")
        self.invoke(fixture)
        self.assertEqual(
            fixture[4].call_args.args[0], "http://127.0.0.1:8088/native-prefix"
        )

    def test_altmount_import_layout_preserves_other_settings_and_requires_no_sync(self):
        fixture = self.fixture("altmount")
        fixture[1]["action"] = "import_layout"
        fixture[2]["arrangement"]["values"].update(
            {
                "altmount::import_strategy": "SYMLINK",
                "altmount::import_dir": "/library/movies",
            }
        )
        self.assertEqual(self.invoke(fixture, inspect=True)["execution"], "planned")
        self.assertFalse(any(call[0] == "PATCH" for call in fixture[3].calls))
        result = self.invoke(fixture)
        self.assertEqual(result["execution"], "applied")
        self.assertEqual(fixture[3].config["import"]["watch_dir"], "/watch")
        self.assertTrue(fixture[3].config["import"]["user_setting"])
        self.assertFalse(result["evidence"]["files_moved"])
        self.assertEqual(
            self.invoke(fixture, inspect=True)["execution"], "already_present"
        )

    def test_import_layout_rejects_system_paths(self):
        fixture = self.fixture("altmount")
        fixture[1]["action"] = "import_layout"
        fixture[2]["arrangement"]["values"].update(
            {"altmount::import_strategy": "STRM", "altmount::import_dir": "/etc/media"}
        )
        self.assertEqual(self.invoke(fixture)["execution"], "waiting_input")

    def test_schema_advertises_only_supported_provider_fields(self):
        fields = providers.schema_fields(
            {"id": "altmount", "service_key": "altmount"}, {}
        )
        self.assertEqual(
            {field["key"] for field in fields},
            {
                "altmount::allow_private_nntp",
                "altmount::import_strategy",
                "altmount::import_dir",
                "altmount::admin_username",
                "altmount::admin_password",
                "altmount::settings_token",
            },
        )
        self.assertEqual(
            providers.schema_fields({"id": "plex", "service_key": "plex"}, {}), []
        )


class ProviderBudgetTests(unittest.TestCase):
    def account(self, **kwargs):
        return {
            "host": "news.example.invalid",
            "port": 563,
            "username": "fixture",
            "password": "private",
            "tls": True,
            "connections": 5,
            **kwargs,
        }

    def payload(self, values):
        return {"arrangement": {"values": values}}

    def test_shared_account_budget_sums_across_services_and_endpoint_aliases(self):
        payload = self.payload(
            {
                "altmount::nntp": [self.account(account_connection_limit=8)],
                "infinidysk::nntp": [self.account()],
            }
        )
        with self.assertRaisesRegex(providers.ProviderInputError, "combined"):
            providers.validate_connection_budgets(payload)
        payload["arrangement"]["values"]["altmount::nntp"][0].update(
            account_id="same-account", account_connection_limit=9
        )
        payload["arrangement"]["values"]["infinidysk::nntp"][0].update(
            host="regional.example.invalid", account_id="same-account"
        )
        with self.assertRaisesRegex(providers.ProviderInputError, "combined"):
            providers.validate_connection_budgets(payload)

    def test_budget_does_not_guess_missing_subscription_limits(self):
        result = providers.validate_connection_budgets(
            self.payload(
                {
                    "altmount::nntp": [self.account()],
                    "infinidysk::nntp": [self.account()],
                }
            )
        )
        self.assertEqual(
            result,
            {
                "requested_connections": 10,
                "shared_accounts": 1,
                "accounts_without_declared_limit": 1,
            },
        )

    def test_duplicate_accounts_control_characters_and_bool_caps_are_rejected(self):
        bad = [
            self.account(password="secret\\r\\nQUIT"),
            self.account(port=True),
            self.account(connections=True),
            self.account(host="https://example.invalid"),
            self.account(tls="false"),
        ]
        bad[0]["password"] = "secret\r\nQUIT"
        for item in bad:
            with (
                self.subTest(item=sorted(item)),
                self.assertRaises(providers.ProviderInputError),
            ):
                providers.validate_connection_budgets(
                    self.payload({"altmount::nntp": [item]})
                )
        with self.assertRaisesRegex(providers.ProviderInputError, "listed twice"):
            providers.validate_connection_budgets(
                self.payload({"altmount::nntp": [self.account(), self.account()]})
            )


class PinnedNntpAuthenticationTests(unittest.TestCase):
    def test_authentication_sends_only_authinfo_and_quit_and_closes_resources(self):
        incoming = io.BytesIO(b"200 Ready\r\n381 Password required\r\n281 Accepted\r\n")
        outgoing = []

        class Stream:
            closed = False

            def readline(self, _limit):
                return incoming.readline()

            def write(self, value):
                outgoing.append(value)

            def flush(self):
                pass

            def close(self):
                self.closed = True

        stream = Stream()
        connection = mock.Mock()
        connection.makefile.return_value = stream
        context = mock.Mock()
        context.minimum_version = providers.ssl.TLSVersion.TLSv1_2
        context.wrap_socket.return_value = connection
        item = {
            "host": "news.example.invalid",
            "port": 563,
            "username": "fixture",
            "password": "private",
            "tls": True,
        }
        with (
            mock.patch.object(
                providers,
                "validate_endpoint",
                return_value=(None, 563, ["203.0.113.1"]),
            ),
            mock.patch.object(
                providers.socket, "create_connection", return_value=connection
            ) as connect,
            mock.patch.object(
                providers.ssl, "create_default_context", return_value=context
            ),
        ):
            self.assertTrue(providers.authenticate_nntp(item))
        connect.assert_called_once_with(("203.0.113.1", 563), timeout=8)
        context.wrap_socket.assert_called_once_with(
            connection, server_hostname="news.example.invalid"
        )
        self.assertEqual(
            outgoing,
            [b"AUTHINFO USER fixture\r\n", b"AUTHINFO PASS private\r\n", b"QUIT\r\n"],
        )
        self.assertTrue(stream.closed)
        connection.close.assert_called_once()


class NntpTlsPolicyTests(unittest.TestCase):
    def test_minimum_is_only_raised_and_verification_policy_is_preserved(self):
        for minimum in (
            providers.ssl.TLSVersion.TLSv1,
            providers.ssl.TLSVersion.TLSv1_2,
            providers.ssl.TLSVersion.TLSv1_3,
        ):
            with self.subTest(minimum=minimum):
                connection = mock.Mock()
                stream = connection.makefile.return_value
                stream.readline.side_effect = [b"200 Ready\r\n", b"281 Accepted\r\n"]
                context = mock.Mock()
                context.minimum_version = minimum
                context.verify_mode = providers.ssl.CERT_REQUIRED
                context.check_hostname = True
                context.wrap_socket.return_value = connection
                item = {
                    "host": "news.example.invalid",
                    "port": 563,
                    "username": "fixture",
                    "password": "private",
                    "tls": True,
                }
                with (
                    mock.patch.object(
                        providers,
                        "validate_endpoint",
                        return_value=(None, 563, ["203.0.113.1"]),
                    ),
                    mock.patch.object(
                        providers.socket, "create_connection", return_value=connection
                    ),
                    mock.patch.object(
                        providers.ssl, "create_default_context", return_value=context
                    ),
                ):
                    self.assertTrue(providers.authenticate_nntp(item))
                self.assertEqual(
                    context.minimum_version,
                    max(minimum, providers.ssl.TLSVersion.TLSv1_2),
                )
                self.assertEqual(context.verify_mode, providers.ssl.CERT_REQUIRED)
                self.assertTrue(context.check_hostname)
                context.wrap_socket.assert_called_once_with(
                    connection, server_hostname="news.example.invalid"
                )
                connection.close.assert_called_once()

    def test_tls_failure_closes_connection_without_plaintext_retry(self):
        connection = mock.Mock()
        context = mock.Mock()
        context.minimum_version = providers.ssl.TLSVersion.TLSv1_3
        context.wrap_socket.side_effect = providers.ssl.SSLError("fixture TLS failure")
        item = {
            "host": "news.example.invalid",
            "port": 563,
            "username": "fixture",
            "password": "private",
            "tls": True,
        }
        with (
            mock.patch.object(
                providers,
                "validate_endpoint",
                return_value=(None, 563, ["203.0.113.1"]),
            ),
            mock.patch.object(
                providers.socket, "create_connection", return_value=connection
            ) as connect,
            mock.patch.object(
                providers.ssl, "create_default_context", return_value=context
            ),
        ):
            with self.assertRaises(providers.ssl.SSLError):
                providers.authenticate_nntp(item)
        connect.assert_called_once()
        connection.makefile.assert_not_called()
        connection.close.assert_called_once()
