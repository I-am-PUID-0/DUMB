import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from utils import arrangement_proxy_routes as adapter
from utils.arrangement_http import ApiError, ApiResponse

TOKEN = "a" * 30 + "." + "b" * 43


class Native:
    def __init__(self):
        self.calls = []
        self.domains = []
        self.services = []
        self.security = {}
        self.role = "admin"
        self.auth_enabled = True
        self.reject_security = False
        self.after_security = None
        self.basic = [
            {
                "id": "basic-1",
                "name": "Existing protected users",
                "users": [
                    {
                        "id": "user-1",
                        "username": "fixture-reader",
                        "configId": "basic-1",
                    }
                ],
            }
        ]
        self.sso = [{"id": "sso-1", "name": "Existing SSO", "enabled": True}]

    def factory(self, origin, **options):
        native = self

        class Client:
            def request(self, method, path, **kwargs):
                native.calls.append((method, path, copy.deepcopy(kwargs)))
                if method != "GET":
                    assert options["headers"]["Origin"] == origin
                if path == "/api/auth/admin/me":
                    if (
                        options.get("headers", {}).get("Cookie")
                        != adapter.COOKIE + "=" + TOKEN
                    ):
                        raise ApiError(status_code=401)
                    data = {
                        "enabled": native.auth_enabled,
                        "session": {
                            "role": native.role,
                            "exp": int(time.time()) + 3600,
                        },
                    }
                elif path == "/api/auth/admin/local/login":
                    return ApiResponse(
                        200,
                        json.dumps(
                            {"ok": True, "user": {"role": native.role}}
                        ).encode(),
                        {
                            "Set-Cookie": adapter.COOKIE
                            + "="
                            + TOKEN
                            + "; HttpOnly; Path=/"
                        },
                    )
                elif path == "/api/domains":
                    if method == "POST":
                        data = {
                            **kwargs["json"],
                            "id": "domain-" + str(len(native.domains) + 1),
                        }
                        native.domains.append(copy.deepcopy(data))
                    else:
                        data = native.domains
                elif path == "/api/services":
                    if method == "POST":
                        data = {
                            **kwargs["json"],
                            "id": "route-" + str(len(native.services) + 1),
                        }
                        native.services.append(copy.deepcopy(data))
                        native.security[data["id"]] = []
                    else:
                        data = native.services
                elif path == "/api/security/basic-auth-configs":
                    data = native.basic
                elif path == "/api/security/sso-configs":
                    data = native.sso
                elif path.startswith("/api/services/"):
                    identifier = path.split("/")[3]
                    service = next(s for s in native.services if s["id"] == identifier)
                    if path.endswith("/security-configs"):
                        if method == "POST":
                            if native.reject_security:
                                raise ApiError(status_code=503)
                            row = kwargs["json"]
                            native.security[identifier].append(
                                {
                                    "type": row["securityType"],
                                    **{
                                        k: row[k]
                                        for k in ("isEnabled", "priority", "config")
                                    },
                                }
                            )
                            if native.after_security:
                                native.after_security()
                        data = native.security[identifier]
                    else:
                        if method == "PUT":
                            service.update(copy.deepcopy(kwargs["json"]))
                        data = {
                            k: v
                            for k, v in service.items()
                            if k not in {"middlewares", "requestHeaders"}
                            or v is not None
                        }
                else:
                    raise AssertionError((method, path))
                return ApiResponse(200, json.dumps(data).encode())

        return Client()


class ProxyRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "package.json").write_text('{"version":"1.24.0"}')
        self.config = {
            "traefik_proxy_admin": {
                "enabled": True,
                "port": 3004,
                "config_dir": str(root),
            },
            "jellyfin": {"enabled": True, "port": 8096},
            "radarr": {"instances": {"HD": {"enabled": True, "port": 7878}}},
            "postgres": {"enabled": True, "port": 5432},
        }
        self.native = Native()
        self.action = {
            "source": adapter.SOURCE,
            "service_key": adapter.SOURCE,
            "action": "proxy_routes",
        }
        self.values = {
            adapter.SOURCE + "::settings_session": TOKEN,
            adapter.SOURCE
            + "::route_domains": [
                {"domain": "example.test", "name": "Selected domain"}
            ],
            adapter.SOURCE
            + "::route_services": [
                {
                    "name": "Jellyfin",
                    "domain": "example.test",
                    "hostname": "media.example.test",
                    "target": "jellyfin",
                    "authentication": "basic:basic-1",
                    "enabled": True,
                }
            ],
        }

    def run_adapter(self, operation="apply", *, inspect=False, action=None):
        method = adapter.inspect_proxy_routes if inspect else adapter.apply_proxy_routes
        return method(
            action or self.action,
            self.config,
            {"arrangement": {"values": self.values}},
            operation,
            client_factory=self.native.factory,
            ownership_directory=self.temp.name,
        )

    def approve(self):
        result = self.run_adapter("preview")
        self.assertEqual(result["evidence"]["preview_status"], "ready", result)
        self.values[adapter.SOURCE + "::adoption_preview_id"] = result["evidence"][
            "preview_id"
        ]
        self.values[adapter.SOURCE + "::adopt_existing"] = True
        self.values[adapter.SOURCE + "::authorize_public_exposure"] = True
        return result

    def writes(self):
        return [row for row in self.native.calls if row[0] != "GET"]

    def test_preview_is_readonly_and_exposure_requires_current_consent(self):
        preview = self.approve()
        self.assertTrue(preview["evidence"]["changes"])
        self.assertFalse(self.writes())
        self.values[adapter.SOURCE + "::authorize_public_exposure"] = False
        self.assertEqual(self.run_adapter()["execution"], "waiting_input")
        self.assertFalse(self.writes())
        self.assertFalse(
            any(path == "/api/traefik/config" for _, path, _ in self.native.calls)
        )

    def test_new_route_is_disabled_until_protection_is_read_back(self):
        self.approve()
        result = self.run_adapter()
        self.assertEqual(
            (result["execution"], result["verification"]), ("applied", "passed"), result
        )
        self.assertFalse(self.native.domains[0]["isDefault"])
        writes = self.writes()
        create = next(
            i for i, r in enumerate(writes) if r[:2] == ("POST", "/api/services")
        )
        protect = next(
            i for i, r in enumerate(writes) if r[1].endswith("/security-configs")
        )
        enable = next(i for i, r in enumerate(writes) if r[0] == "PUT")
        self.assertLess(create, protect)
        self.assertLess(protect, enable)
        self.assertFalse(writes[create][2]["json"]["enabled"])
        self.assertTrue(writes[enable][2]["json"]["enabled"])
        self.assertFalse(result["evidence"]["public_route_verified"])
        count = len(self.writes())
        self.assertEqual(self.run_adapter()["execution"], "already_present")
        self.assertEqual(
            self.run_adapter("recheck", inspect=True)["execution"], "already_present"
        )
        self.assertEqual(count, len(self.writes()))

    def test_failed_security_never_enables_route_or_blindly_retries(self):
        self.approve()
        self.native.reject_security = True
        result = self.run_adapter()
        self.assertEqual(result["execution"], "waiting_input")
        self.assertFalse(self.native.services[0]["enabled"])
        self.assertFalse(any(r[0] == "PUT" for r in self.writes()))
        self.assertEqual(self.run_adapter()["execution"], "waiting_input")
        self.assertEqual(len(self.native.services), 1)

    def test_owned_disabled_partial_route_recovers_only_after_fresh_review(self):
        self.approve()
        self.native.reject_security = True
        self.assertEqual(self.run_adapter()["execution"], "waiting_input")
        self.native.reject_security = False
        count = len(self.writes())
        self.assertEqual(self.run_adapter()["execution"], "waiting_input")
        self.assertEqual(count, len(self.writes()))
        self.approve()
        self.assertEqual(self.run_adapter()["execution"], "applied")
        self.assertEqual(len(self.native.services), 1)
        self.assertTrue(self.native.services[0]["enabled"])

    def test_partial_route_native_drift_does_not_reuse_ownership_authority(self):
        self.approve()
        self.native.reject_security = True
        self.run_adapter()
        self.native.reject_security = False
        self.native.services[0]["requestHeaders"] = {"X-External-Change": "preserve"}
        count = len(self.writes())
        self.assertEqual(self.run_adapter("preview")["execution"], "waiting_input")
        self.assertEqual(count, len(self.writes()))
        self.assertFalse(self.native.services[0]["enabled"])

    def test_stale_inputs_and_native_drift_invalidate_acceptance(self):
        self.approve()
        self.values[adapter.SOURCE + "::route_services"][0][
            "name"
        ] = "Changed after review"
        self.assertEqual(self.run_adapter()["execution"], "waiting_input")
        self.assertFalse(self.writes())
        self.approve()
        self.assertEqual(self.run_adapter()["execution"], "applied")
        self.values[adapter.SOURCE + "::route_services"][0][
            "name"
        ] = "Reviewed replacement"
        self.approve()
        count = len(self.writes())
        self.native.services[0]["requestHeaders"] = {"X-User-Header": "keep"}
        self.assertEqual(self.run_adapter()["execution"], "waiting_input")
        self.assertEqual(len(self.writes()), count)

    def test_existing_domain_defaults_headers_and_security_are_preserved(self):
        self.approve()
        self.assertEqual(self.run_adapter()["execution"], "applied")
        self.native.domains[0].update(isDefault=True, certResolver="user-resolver")
        self.native.services[0].update(
            serviceGroup="User group",
            requestHeaders={"X-User": "keep"},
            middlewares=["existing-security"],
            enableDurationMinutes=60,
        )
        self.values[adapter.SOURCE + "::route_services"][0].update(
            name="Reviewed new name", authentication="preserve", target="radarr.HD"
        )
        policies = copy.deepcopy(self.native.security)
        self.approve()
        result = self.run_adapter()
        self.assertEqual(result["execution"], "applied", result)
        self.assertEqual(self.native.security, policies)
        self.assertEqual(self.native.services[0]["requestHeaders"], {"X-User": "keep"})
        self.assertEqual(self.native.services[0]["middlewares"], ["existing-security"])
        self.assertEqual(self.native.services[0]["enableDurationMinutes"], 60)
        self.assertEqual(self.native.domains[0]["certResolver"], "user-resolver")
        self.assertTrue(self.native.domains[0]["isDefault"])

    def test_unselected_native_route_is_untouched(self):
        self.approve()
        self.assertEqual(self.run_adapter()["execution"], "applied")
        other = copy.deepcopy(self.native.services[0])
        other.update(
            id="user-route",
            name="Unselected",
            customHostnames=["other.example.test"],
            targetIp="192.0.2.9",
            targetPort=1234,
        )
        self.native.services.append(other)
        self.native.security["user-route"] = [
            {
                "type": "bypass",
                "isEnabled": True,
                "priority": 3,
                "config": {
                    "name": "User rule",
                    "rule": "PathPrefix(`/private`)",
                    "mode": "simple",
                    "middlewares": [],
                    "sessionDurationMinutes": 0,
                },
            }
        ]
        original = copy.deepcopy((other, self.native.security["user-route"]))
        self.values[adapter.SOURCE + "::route_services"][0]["name"] = "Selected change"
        self.approve()
        self.assertEqual(self.run_adapter()["execution"], "applied")
        self.assertEqual(original, (other, self.native.security["user-route"]))

    def test_existing_protection_cannot_be_weakened_or_extended_as_alternative(self):
        self.approve()
        self.assertEqual(self.run_adapter()["execution"], "applied")
        count = len(self.writes())
        for policy in ("application", "sso:sso-1"):
            with self.subTest(policy=policy):
                self.values[adapter.SOURCE + "::route_services"][0].update(
                    authentication=policy, allow_all_sso_users=True
                )
                self.assertEqual(
                    self.run_adapter("preview")["execution"], "waiting_input"
                )
                self.assertEqual(len(self.writes()), count)

    def test_empty_or_unknown_basic_profile_cannot_enable_or_verify_protection(self):
        for users in (
            [],
            None,
            [{"id": "user-1", "username": "fixture", "configId": "different"}],
            [
                {
                    "id": "user-1",
                    "username": "fixture",
                    "configId": "basic-1",
                    "disabled": True,
                }
            ],
        ):
            with self.subTest(users=users):
                self.native.basic[0]["users"] = users
                self.assertEqual(
                    self.run_adapter("preview")["execution"], "waiting_input"
                )
                self.assertFalse(self.writes())

    def test_last_basic_user_removed_before_enable_leaves_route_disabled(self):
        self.approve()
        self.native.after_security = lambda: self.native.basic[0].update(users=[])
        result = self.run_adapter()
        self.assertEqual(result["execution"], "waiting_input")
        self.assertFalse(self.native.services[0]["enabled"])
        self.assertFalse(any(row[0] == "PUT" for row in self.writes()))

    def test_preserved_basic_policy_is_not_verified_after_last_user_removed(self):
        self.approve()
        self.assertEqual(self.run_adapter()["execution"], "applied")
        self.values[adapter.SOURCE + "::route_services"][0][
            "authentication"
        ] = "preserve"
        self.native.basic[0]["users"] = []
        count = len(self.writes())
        result = self.run_adapter("recheck", inspect=True)
        self.assertEqual(
            (result["execution"], result["verification"]),
            ("waiting_input", "unavailable"),
        )
        self.assertEqual(count, len(self.writes()))

    def test_sso_all_users_needs_separate_explicit_choice(self):
        self.values[adapter.SOURCE + "::route_services"][0][
            "authentication"
        ] = "sso:sso-1"
        self.assertNotIn("preview_id", self.run_adapter("preview")["evidence"])
        self.assertFalse(self.writes())
        self.values[adapter.SOURCE + "::route_services"][0][
            "sso_groups"
        ] = "media-viewers"
        self.approve()
        self.assertEqual(self.run_adapter()["execution"], "applied")
        self.assertEqual(
            self.native.security["route-1"][0]["config"]["groups"], ["media-viewers"]
        )

    def test_private_arbitrary_or_unselected_targets_and_ambiguous_hosts_refused(self):
        for changes in (
            {"target": "postgres"},
            {"target": "http://metadata.invalid"},
            {"hostname": "other.invalid"},
            {"hostname": "https://media.example.test"},
        ):
            with self.subTest(changes=changes):
                original = copy.deepcopy(
                    self.values[adapter.SOURCE + "::route_services"]
                )
                self.values[adapter.SOURCE + "::route_services"][0].update(changes)
                self.assertEqual(
                    self.run_adapter("preview")["execution"], "waiting_input"
                )
                self.assertFalse(self.writes())
                self.values[adapter.SOURCE + "::route_services"] = original

    def test_distinct_dns_names_cannot_overwrite_same_native_router_identifier(self):
        row = self.values[adapter.SOURCE + "::route_services"][0]
        row["hostname"] = "a.b-c.example.test"
        second = {**row, "name": "Other", "hostname": "a-b.c.example.test"}
        self.values[adapter.SOURCE + "::route_services"].append(second)
        self.assertEqual(self.run_adapter("preview")["execution"], "waiting_input")
        self.assertFalse(self.writes())
        self.values[adapter.SOURCE + "::route_services"].pop()
        self.approve()
        self.assertEqual(self.run_adapter()["execution"], "applied")
        row["hostname"] = second["hostname"]
        count = len(self.writes())
        self.assertEqual(self.run_adapter("preview")["execution"], "waiting_input")
        self.assertEqual(len(self.writes()), count)

    def test_derived_native_router_namespaces_cannot_overlap_selected_base(self):
        self.approve()
        self.assertEqual(self.run_adapter()["execution"], "applied")
        other = copy.deepcopy(self.native.services[0])
        other.update(
            id="operator-route", hostnameMode="subdomain", customHostnames=None
        )
        self.native.services.append(other)
        self.native.security["operator-route"] = []
        count = len(self.writes())
        for suffix in ("tpa-auth-ticket", "bypass-user", "ip-jail-1", "advanced-route"):
            with self.subTest(suffix=suffix):
                other["subdomain"] = "media-example-test-" + suffix
                result = self.run_adapter("preview")
                self.assertEqual(result["execution"], "waiting_input")
                self.assertEqual(len(self.writes()), count)

    def test_global_managed_middlewares_cannot_shadow_generated_protection(self):
        self.approve()
        self.assertEqual(self.run_adapter()["execution"], "applied")
        other = copy.deepcopy(self.native.services[0])
        other.update(id="operator-route", customHostnames=["other.example.test"])
        self.native.services.append(other)
        self.native.security["operator-route"] = []
        count = len(self.writes())
        for name in (
            "auth-sso-media-example-test",
            "headers-media-example-test",
            "basic auth media example test user",
            "observe-bypass-media-example-test-user",
        ):
            with self.subTest(name=name):
                other["managedMiddlewares"] = {
                    name: {"headers": {"customResponseHeaders": {"X-Lab": "keep"}}}
                }
                result = self.run_adapter("preview")
                self.assertEqual(result["execution"], "waiting_input")
                self.assertEqual(len(self.writes()), count)
        other["managedMiddlewares"] = {"unrelated-user-policy": {"headers": {}}}
        self.assertEqual(
            self.run_adapter("recheck", inspect=True)["execution"], "already_present"
        )

    def test_certificate_trigger_hostnames_require_native_policy_review(self):
        self.approve()
        self.assertEqual(self.run_adapter()["execution"], "applied")
        domain = self.native.domains[0]
        count = len(self.writes())
        for cert in (
            {"name": "Main", "main": "media.example.test", "certResolver": "native"},
            {
                "name": "SAN",
                "main": "other.example.test",
                "sans": ["media.example.test"],
                "certResolver": "native",
            },
        ):
            with self.subTest(cert=cert):
                domain["certificateConfigs"] = json.dumps([cert])
                self.assertEqual(
                    self.run_adapter("preview")["execution"], "waiting_input"
                )
                self.assertEqual(len(self.writes()), count)
        domain["certificateConfigs"] = json.dumps(
            [{"main": "media.example.test", "certResolver": "none"}]
        )
        self.assertEqual(
            self.run_adapter("recheck", inspect=True)["execution"], "already_present"
        )
        domain.update(
            certificateConfigs=None, useWildcardCert=True, certResolver="native"
        )
        self.values[adapter.SOURCE + "::route_services"][0].update(
            hostname="example.test", authentication="application"
        )
        self.assertEqual(self.run_adapter("preview")["execution"], "waiting_input")
        self.assertEqual(len(self.writes()), count)

    def test_shared_certificate_change_after_staging_never_enables_route(self):
        self.approve()
        self.native.after_security = lambda: self.native.domains[0].update(
            certificateConfigs=[
                {
                    "name": "Concurrent",
                    "main": "media.example.test",
                    "certResolver": "native",
                }
            ]
        )
        result = self.run_adapter()
        self.assertEqual(result["execution"], "waiting_input")
        self.assertFalse(self.native.services[0]["enabled"])
        self.assertFalse(any(row[0] == "PUT" for row in self.writes()))

    def test_staged_user_change_after_protection_is_not_overwritten(self):
        self.approve()
        self.native.after_security = lambda: self.native.services[0].update(
            requestHeaders={"X-Concurrent-User": "keep"}
        )
        result = self.run_adapter()
        self.assertEqual(result["execution"], "waiting_input")
        self.assertFalse(self.native.services[0]["enabled"])
        self.assertEqual(
            self.native.services[0]["requestHeaders"], {"X-Concurrent-User": "keep"}
        )
        self.assertFalse(any(row[0] == "PUT" for row in self.writes()))

    def test_account_only_apply_logs_in_and_retains_protected_session(self):
        self.values.pop(adapter.SOURCE + "::settings_session")
        self.values.update(
            {
                adapter.SOURCE + "::admin_username": "lab-admin",
                adapter.SOURCE + "::admin_password": "fake-password",
            }
        )
        action = {**self.action, "action": "account"}
        for operation in ("preview", "recheck"):
            self.assertEqual(
                self.run_adapter(operation, action=action)["execution"], "waiting_input"
            )
            self.assertFalse(self.writes())
        result = self.run_adapter(action=action)
        self.assertEqual(result["verification"], "passed", result)
        self.assertEqual(
            result["_private_values"][adapter.SOURCE + "::settings_session"], TOKEN
        )
        self.assertIn(adapter.SOURCE + "::admin_password", result["_clear_inputs"])

    def test_disabled_auth_lesser_roles_and_unknown_versions_fail_closed(self):
        for role, enabled in (("viewer", True), ("editor", True), ("admin", False)):
            with self.subTest(role=role, enabled=enabled):
                self.native.role, self.native.auth_enabled = role, enabled
                self.assertEqual(
                    self.run_adapter("preview")["execution"], "waiting_input"
                )
                self.assertFalse(self.writes())
        (Path(self.temp.name) / "package.json").write_text('{"version":"2.0.0"}')
        self.native.calls.clear()
        self.assertEqual(self.run_adapter("preview")["verification"], "unavailable")
        self.assertFalse(self.native.calls)


if __name__ == "__main__":
    unittest.main()
