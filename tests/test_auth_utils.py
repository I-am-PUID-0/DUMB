import importlib
import types
import unittest
from datetime import datetime, timedelta, timezone

try:
    auth = importlib.import_module("utils.auth")
except ImportError:  # pragma: no cover - host fallback; CI/devcontainer installs deps
    auth = None


@unittest.skipIf(auth is None, "auth dependencies are not installed")
class AuthUtilityTests(unittest.TestCase):
    def setUp(self):
        self.original_auth_config = auth._auth_config_manager
        auth._auth_config_manager = types.SimpleNamespace(
            get_jwt_secret=lambda: "test-secret-with-at-least-32-bytes"
        )

    def tearDown(self):
        auth._auth_config_manager = self.original_auth_config

    def test_create_and_decode_access_token(self):
        token = auth.create_access_token("alice", expires_delta=timedelta(minutes=5))

        payload = auth.decode_token(token)

        self.assertIsNotNone(payload)
        self.assertEqual(payload.sub, "alice")
        self.assertEqual(payload.type, "access")
        self.assertGreater(payload.exp, datetime.now(timezone.utc))

    def test_create_and_decode_refresh_token(self):
        token = auth.create_refresh_token("alice", expires_delta=timedelta(days=1))

        payload = auth.decode_token(token)

        self.assertIsNotNone(payload)
        self.assertEqual(payload.sub, "alice")
        self.assertEqual(payload.type, "refresh")

    def test_decode_token_rejects_expired_token(self):
        token = auth.create_access_token("alice", expires_delta=timedelta(seconds=-1))

        self.assertIsNone(auth.decode_token(token))

    def test_decode_token_rejects_invalid_token(self):
        self.assertIsNone(auth.decode_token("not-a-valid-token"))

    def test_create_token_pair_returns_bearer_pair(self):
        pair = auth.create_token_pair("alice")

        self.assertEqual(pair.token_type, "bearer")
        self.assertEqual(auth.decode_token(pair.access_token).type, "access")
        self.assertEqual(auth.decode_token(pair.refresh_token).type, "refresh")

    def test_password_hash_verifies_correct_password_only(self):
        hashed = auth.get_password_hash("correct horse battery staple")

        self.assertTrue(auth.verify_password("correct horse battery staple", hashed))
        self.assertFalse(auth.verify_password("wrong horse battery staple", hashed))

    def test_password_hash_and_verify_apply_bcrypt_72_byte_limit(self):
        password = "a" * 72 + "first suffix"
        same_bcrypt_input = "a" * 72 + "second suffix"
        hashed = auth.get_password_hash(password)

        self.assertTrue(auth.verify_password(same_bcrypt_input, hashed))


if __name__ == "__main__":
    unittest.main()
