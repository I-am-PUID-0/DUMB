import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from utils.arrangement_mediastorm import _engine


class MediaStormCredentialDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "altmount": {
                "enabled": True,
                "port": 8088,
                "env": {"ALTMOUNT_API_KEY": "fixture-alt-api-key"},
            }
        }
        self.values = {"mediastorm::usenet_engine": "altmount"}

    def test_selected_altmount_uses_only_live_verified_native_credentials(self):
        with patch(
            "utils.arrangement_altmount_account.verified_webdav_credentials",
            return_value={
                "username": "verified-user",
                "password": "private-verified-password",
            },
        ) as verified:
            result = _engine(self.config, "mediastorm", self.values)
        verified.assert_called_once()
        self.assertEqual("verified-user", result["webdavUsername"])
        self.assertEqual("private-verified-password", result["webdavPassword"])

    def test_native_altmount_webdav_path_is_defaulted_but_user_override_wins(self):
        self.values.update(
            {
                "mediastorm::engine_webdav_username": "user",
                "mediastorm::engine_webdav_password": "private-password",
            }
        )
        self.assertEqual(
            "http://127.0.0.1:8088/webdav",
            _engine(self.config, "mediastorm", self.values)["webdavBaseUrl"],
        )
        self.values["mediastorm::engine_webdav_path"] = "/explicit-webdav"
        self.assertEqual(
            "http://127.0.0.1:8088/explicit-webdav",
            _engine(self.config, "mediastorm", self.values)["webdavBaseUrl"],
        )

    def test_decypharr_native_base_and_private_key_are_used_for_selected_engine(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path / "config.json").write_text(json.dumps({"url_base": "/native-dec/"}))
            (path / "auth.json").write_text(
                json.dumps({"api_token": "private-native-key"})
            )
            self.config["decypharr"] = {
                "enabled": True,
                "port": 8282,
                "config_dir": root,
            }
            self.values.update(
                {
                    "mediastorm::usenet_engine": "decypharr",
                    "mediastorm::engine_webdav_username": "chosen-user",
                    "mediastorm::engine_webdav_password": "chosen-password",
                    "mediastorm::engine_webdav_path": "/webdav",
                }
            )
            engine = _engine(self.config, "mediastorm", self.values)
            self.assertEqual("http://127.0.0.1:8282/native-dec", engine["baseUrl"])
            self.assertEqual(
                "http://127.0.0.1:8282/native-dec/webdav", engine["webdavBaseUrl"]
            )
            self.assertEqual("private-native-key", engine["apiKey"])

    def test_explicit_playback_credentials_are_preserved(self):
        self.values.update(
            {
                "mediastorm::engine_webdav_username": "chosen-user",
                "mediastorm::engine_webdav_password": "chosen-private-password",
            }
        )
        with patch(
            "utils.arrangement_altmount_account.verified_webdav_credentials",
            side_effect=AssertionError("explicit override must be preserved"),
        ):
            result = _engine(self.config, "mediastorm", self.values)
        self.assertEqual("chosen-user", result["webdavUsername"])
        self.assertEqual("chosen-private-password", result["webdavPassword"])
