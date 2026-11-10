import json
from pathlib import Path
import tempfile
import unittest

from utils.arrangement_adoption import preview


class AdoptionPrivacyTests(unittest.TestCase):
    def test_nested_native_secrets_and_credential_urls_never_enter_public_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            result = preview(
                Path(directory) / "preview.json",
                "fixture",
                {
                    "native.registration": {
                        "url": "https://user:private-password@example.invalid/api"
                    },
                    "native.others": [
                        {"api_key": "private-other-key", "enabled": True}
                    ],
                },
                {
                    "native.registration": {"url": "https://safe.example.invalid/api"},
                    "native.others": [{"api_key": "new-private-key", "enabled": True}],
                },
            )
            public = json.dumps(result)
            for secret in (
                "private-password",
                "private-other-key",
                "new-private-key",
                "user:",
            ):
                self.assertNotIn(secret, public)
            self.assertTrue(all(row["secret"] for row in result["changes"]))
            self.assertTrue(result["preview_id"])
