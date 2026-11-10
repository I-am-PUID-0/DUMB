import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from utils import arrangement_altmount_account as adapter
from utils.arrangement_http import ApiResponse
from utils.zurg_config import _load_document
from io import StringIO


class Native:
    def __init__(self, path):
        self.path = path
        self.users = {}
        self.calls = []
        self.status_override = None
        self.reject_login = False
        self.reject_dav = False
        self.anonymous_dav = False
        self.ignore_write = False
        self.lose_registration_response = False
        self.session = "eyJhbGciOiJIUzI1NiJ9.disposable.signature"

    def response(self, data, status=200):
        return ApiResponse(status, json.dumps(data).encode())

    def document(self):
        return _load_document(self.path.read_text())[1]

    def factory(self, url, **options):
        native = self

        class Client:
            def __init__(self):
                self.cookies = {}

            def request(self, method, path, **kwargs):
                native.calls.append(
                    (method, path, copy.deepcopy(options), copy.deepcopy(kwargs))
                )
                if path == "/api/auth/registration-status":
                    doc = native.status_override or {
                        "user_count": len(native.users),
                        "registration_enabled": not native.users,
                    }
                    return native.response({"success": True, "data": doc})
                if path == "/api/auth/register":
                    if native.users:
                        return native.response({}, 409)
                    body = kwargs["json"]
                    native.users[body["username"]] = body["password"]
                    if native.lose_registration_response:
                        return native.response({}, 503)
                    return native.response(
                        {"success": True, "data": {"user": {"is_admin": True}}}
                    )
                if path == "/api/auth/login":
                    body = kwargs["json"]
                    if native.reject_login:
                        return native.response({}, 503)
                    if native.users.get(body["username"]) != body["password"]:
                        return native.response({}, 401)
                    self.cookies["JWT"] = native.session
                    return native.response(
                        {"success": True, "data": {"user": {"is_admin": True}}}
                    )
                if path == "/api/config":
                    if (options.get("headers") or {}).get(
                        "Cookie"
                    ) != "JWT=" + native.session:
                        return native.response({}, 401)
                    doc = native.document()
                    webdav = dict(doc["webdav"])
                    webdav["password"] = "********"
                    return native.response(
                        {"success": True, "data": {"providers": [], "webdav": webdav}}
                    )
                if path == "/api/config/webdav":
                    if not native.ignore_write:
                        parser, doc = _load_document(native.path.read_text())
                        doc["webdav"].update(kwargs["json"]["webdav"])
                        output = StringIO()
                        parser.dump(doc, output)
                        native.path.write_text(output.getvalue())
                    return native.response({"success": True})
                if method == "PROPFIND":
                    self_credentials = native.document()["webdav"]
                    expected = (
                        "Basic "
                        + base64.b64encode(
                            (
                                self_credentials["user"]
                                + ":"
                                + self_credentials["password"]
                            ).encode()
                        ).decode()
                    )
                    supplied = (options.get("headers") or {}).get("Authorization")
                    allowed = native.anonymous_dav or (
                        supplied == expected and not native.reject_dav
                    )
                    if not allowed:
                        return ApiResponse(401, b"Unauthorized")
                    return ApiResponse(
                        207,
                        b'<multistatus xmlns="DAV:"><response><href>/webdav/</href><propstat><status>HTTP/1.1 200 OK</status></propstat></response></multistatus>',
                    )
                raise AssertionError((method, path))

        return Client()


class AltMountAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.yaml"
        self.path.write_text(
            "# Keep native comment\nwebdav:\n  port: 8088\n  user: usenet\n  password: usenet\n  host: https://dav.example.com\nauth:\n  login_required: true\nproviders: []\n"
        )
        self.native = Native(self.path)
        self.config = {
            "altmount": {
                "enabled": True,
                "port": 8088,
                "mount_type": "none",
                "config_dir": self.temp.name,
                "config_file": str(self.path),
                "env": {"ALTMOUNT_API_KEY": "fixture-api-key"},
            }
        }
        self.action = {
            "source": "altmount",
            "service_key": "altmount",
            "action": "account",
        }
        self.values = {
            "altmount::admin_username": "operator",
            "altmount::admin_password": "private-admin-password",
        }

    def call(self, apply=False, operation=None, stopped=False):
        return (
            adapter.apply_altmount_account
            if apply
            else adapter.inspect_altmount_account
        )(
            self.action,
            self.config,
            {"arrangement": {"values": self.values}, "_service_stopped": stopped},
            operation,
            client_factory=self.native.factory,
        )

    def authorized(self):
        self.native.users = {"operator": "private-admin-password"}
        self.values.pop("altmount::admin_password", None)
        self.values["altmount::settings_token"] = self.native.session
        self.action["action"] = "webdav"

    def propose(self):
        self.values.update(
            {
                "altmount::webdav_username": "chosen-user",
                "altmount::webdav_password": "private-webdav-password",
                "altmount::acknowledge_external_consumers": True,
            }
        )
        result = self.call(operation="preview")
        self.values["altmount::adopt_existing"] = True
        self.values["altmount::adoption_preview_id"] = result["evidence"]["preview_id"]
        return result

    def test_first_administrator_needs_explicit_consent_then_hands_off_verified_session(
        self,
    ):
        self.assertEqual("waiting_input", self.call(True)["execution"])
        self.assertEqual({}, self.native.users)
        self.values["altmount::create_admin"] = True
        self.assertEqual("planned", self.call()["execution"])
        self.assertFalse(any(call[0] == "POST" for call in self.native.calls))
        result = self.call(True)
        self.assertEqual(
            ("applied", "passed"), (result["execution"], result["verification"])
        )
        self.assertEqual(
            self.native.session, result["_private_values"]["altmount::settings_token"]
        )
        self.assertEqual(["altmount::admin_password"], result["_clear_inputs"])
        self.assertNotIn("private-admin-password", result["reason"])
        self.values.update(result["_private_values"])
        self.values.pop("altmount::admin_password")
        self.native.calls.clear()
        self.assertEqual("already_present", self.call(True)["execution"])
        self.assertFalse(
            any(call[1] == "/api/auth/register" for call in self.native.calls)
        )

    def test_lost_registration_response_reuses_created_account_on_retry(self):
        self.values["altmount::create_admin"] = True
        self.native.lose_registration_response = True
        result = self.call(True)
        self.assertEqual("waiting_dependency", result["execution"])
        self.assertNotIn("_clear_inputs", result)
        self.native.lose_registration_response = False
        self.assertEqual("already_present", self.call(True)["execution"])
        self.assertEqual(
            1, sum(call[1] == "/api/auth/register" for call in self.native.calls)
        )

    def test_concurrent_first_user_is_preserved_by_fresh_guard(self):
        self.values["altmount::create_admin"] = True
        count = [0]
        original = adapter._status

        def status(factory, cfg):
            count[0] += 1
            if count[0] == 2:
                self.native.users = {"another-owner": "another-private-password"}
            return original(factory, cfg)

        with patch.object(adapter, "_status", side_effect=status):
            self.assertEqual("waiting_input", self.call(True)["execution"])
        self.assertEqual(
            {"another-owner": "another-private-password"}, self.native.users
        )
        self.assertFalse(
            any(call[1] == "/api/auth/register" for call in self.native.calls)
        )

    def test_consumer_defaults_require_current_native_auth_and_ignore_pending_inputs(
        self,
    ):
        self.authorized()
        self.assertIsNone(
            adapter.verified_webdav_credentials(
                self.config, client_factory=self.native.factory
            )
        )
        self.path.write_text(
            self.path.read_text()
            .replace("user: usenet", "user: existing")
            .replace("password: usenet", "password: existing-password")
        )
        self.values.update(
            {
                "altmount::webdav_username": "pending-user",
                "altmount::webdav_password": "pending-password",
            }
        )
        self.assertEqual(
            {"username": "existing", "password": "existing-password"},
            adapter.verified_webdav_credentials(
                self.config, client_factory=self.native.factory
            ),
        )
        self.native.reject_dav = True
        self.assertIsNone(
            adapter.verified_webdav_credentials(
                self.config, client_factory=self.native.factory
            )
        )
        self.assertFalse(
            any(call[0] in {"POST", "PATCH"} for call in self.native.calls)
        )

    def test_existing_account_is_never_reinitialized(self):
        self.native.users = {"operator": "private-admin-password"}
        self.values["altmount::create_admin"] = True
        self.assertEqual("already_present", self.call(True)["execution"])
        self.assertFalse(
            any(call[1] == "/api/auth/register" for call in self.native.calls)
        )
        self.assertEqual({"operator": "private-admin-password"}, self.native.users)

    def test_unknown_or_conflicting_fresh_guard_blocks_registration(self):
        for status in (
            {"user_count": 0, "registration_enabled": "true"},
            {"user_count": 1, "registration_enabled": True},
        ):
            self.native.status_override = status
            self.values["altmount::create_admin"] = True
            self.assertEqual("waiting_input", self.call(True)["execution"])
            self.assertFalse(self.native.users)

    def test_created_account_pending_session_keeps_temporary_password_for_safe_retry(
        self,
    ):
        self.values["altmount::create_admin"] = True
        self.native.reject_login = True
        result = self.call(True)
        self.assertEqual("waiting_dependency", result["execution"])
        self.assertNotIn("_clear_inputs", result)
        self.assertEqual(1, len(self.native.users))
        self.native.reject_login = False
        self.assertEqual("already_present", self.call(True)["execution"])
        self.assertEqual(
            1, sum(call[1] == "/api/auth/register" for call in self.native.calls)
        )

    def test_existing_custom_credentials_are_reused_and_only_depth_zero_metadata_is_read(
        self,
    ):
        self.authorized()
        self.path.write_text(
            self.path.read_text()
            .replace("user: usenet", "user: existing")
            .replace("password: usenet", "password: existing-password")
        )
        result = self.call(True)
        self.assertEqual("passed", result["verification"])
        self.assertEqual(
            "existing-password", result["_private_values"]["altmount::webdav_password"]
        )
        self.assertFalse(any(call[0] == "PATCH" for call in self.native.calls))
        dav = [call for call in self.native.calls if call[0] == "PROPFIND"]
        self.assertEqual(2, len(dav))
        self.assertTrue(
            all(
                call[1] == "/webdav/" and call[3]["headers"]["Depth"] == "0"
                for call in dav
            )
        )

    def test_default_credentials_remain_pending_until_exact_adoption_and_readback(self):
        self.authorized()
        self.assertEqual("waiting_input", self.call(True)["execution"])
        before = self.path.read_bytes()
        preview = self.propose()
        self.assertEqual("waiting_input", preview["execution"])
        self.assertTrue(preview["evidence"]["requires_consent"])
        self.assertNotIn("private-webdav-password", json.dumps(preview))
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual("planned", self.call()["execution"])
        self.assertEqual("passed", self.call(True)["verification"])
        doc = self.native.document()
        self.assertTrue(doc["auth"]["login_required"])
        self.assertEqual("https://dav.example.com", doc["webdav"]["host"])
        self.assertEqual(8088, doc["webdav"]["port"])
        self.assertEqual("already_present", self.call(True)["execution"])

    def test_native_credential_drift_invalidates_accepted_preview(self):
        self.authorized()
        self.propose()
        self.path.write_text(
            self.path.read_text().replace(
                "password: usenet", "password: user-edited-password"
            )
        )
        before = self.path.read_bytes()
        self.assertEqual("waiting_input", self.call(True)["execution"])
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(any(call[0] == "PATCH" for call in self.native.calls))

    def test_internal_rclone_requires_controlled_stop_and_preserves_native_document(
        self,
    ):
        self.authorized()
        self.propose()
        self.config["altmount"]["mount_type"] = "rclone"
        self.assertTrue(self.call()["evidence"]["controlled_restart_required"])
        before = self.path.read_bytes()
        self.assertEqual("waiting_input", self.call(True)["execution"])
        self.assertEqual(before, self.path.read_bytes())
        result = self.call(True, stopped=True)
        self.assertEqual(
            ("applied", "not_checked"), (result["execution"], result["verification"])
        )
        self.assertIn("# Keep native comment", self.path.read_text())
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.assertFalse(any(call[0] == "PATCH" for call in self.native.calls))
        self.assertEqual("passed", self.call()["verification"])

    def test_ignored_native_write_and_bad_authentication_are_not_reported_successful(
        self,
    ):
        self.authorized()
        self.propose()
        self.native.ignore_write = True
        self.assertEqual("waiting_input", self.call(True)["execution"])
        self.native.ignore_write = False
        self.native.reject_dav = True
        self.assertEqual("waiting_input", self.call(True)["execution"])
        self.native.reject_dav = False
        self.native.anonymous_dav = True
        self.assertEqual("waiting_input", self.call(True)["execution"])

    def test_external_consumer_acknowledgment_is_required_even_after_adoption(self):
        self.authorized()
        self.propose()
        self.values["altmount::acknowledge_external_consumers"] = False
        before = self.path.read_bytes()
        self.assertEqual("waiting_input", self.call(True)["execution"])
        self.assertEqual(before, self.path.read_bytes())
