"""Private ownership reads preserve legacy identities and fail closed on unsafe records."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from utils.integration_ownership import fingerprint, read_ownership, write_ownership


class IntegrationOwnershipTests(unittest.TestCase):
    def test_legacy_fingerprint_and_private_roundtrip_are_unchanged(self):
        value = {
            "url": "http://127.0.0.1:7878",
            "key": "reserved-fixture",
            "family": "radarr",
        }
        expected = hashlib.sha256(
            b'{"family":"radarr","key":"reserved-fixture","url":"http://127.0.0.1:7878"}'
        ).hexdigest()
        self.assertEqual(fingerprint(value), expected)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "owner.json"
            records = {"scope": expected, "registration": {"id": 12}}
            write_ownership(path, records)
            self.assertEqual(read_ownership(path), records)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_is_empty_but_unsafe_existing_ownership_is_never_forgotten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "owner.json"
            self.assertEqual(read_ownership(path), {})
            content = json.dumps({"scope": "legacy-binding", "id": 12})
            path.write_text(content)
            path.chmod(0o644)
            with self.assertRaises(PermissionError):
                read_ownership(path)
            self.assertEqual(path.read_text(), content)
            path.chmod(0o600)
            self.assertEqual(read_ownership(path)["id"], 12)

    def test_invalid_existing_record_is_an_error_and_remains_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "owner.json"
            write_ownership(path, [])
            with self.assertRaises(ValueError):
                read_ownership(path)
            self.assertEqual(json.loads(path.read_text()), [])

    def test_unavailable_parent_alias_is_not_treated_as_missing_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            alias = Path(temporary) / "managed-app"
            alias.symlink_to(
                Path(temporary) / "unavailable-data", target_is_directory=True
            )
            with self.assertRaisesRegex(
                PermissionError, "unavailable managed path alias"
            ):
                read_ownership(alias / ".dumb-ownership.json")
            self.assertTrue(alias.is_symlink())
