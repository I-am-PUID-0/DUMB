import os
import tempfile
import unittest
from pathlib import Path

from utils.mediastorm_credentials import (
    MAX_INITIAL_ADMIN_PASSWORD_BYTES,
    MediaStormCredentialError,
    read_initial_admin_password,
)


class MediaStormCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.cache_dir = self.config_dir / "cache"
        self.cache_dir.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_missing_credential_returns_none(self):
        self.assertIsNone(read_initial_admin_password(str(self.config_dir)))

    def test_reads_txt_bootstrap_credential(self):
        (self.cache_dir / "initial_admin_password.txt").write_text(
            "generated-secret\n",
            encoding="utf-8",
        )

        self.assertEqual(
            read_initial_admin_password(str(self.config_dir)),
            "generated-secret",
        )

    def test_supports_extensionless_upstream_filename(self):
        (self.cache_dir / "initial_admin_password").write_text(
            "upstream-secret",
            encoding="utf-8",
        )

        self.assertEqual(
            read_initial_admin_password(str(self.config_dir)),
            "upstream-secret",
        )

    def test_prefers_txt_filename_when_both_exist(self):
        (self.cache_dir / "initial_admin_password.txt").write_text(
            "txt-secret",
            encoding="utf-8",
        )
        (self.cache_dir / "initial_admin_password").write_text(
            "extensionless-secret",
            encoding="utf-8",
        )

        self.assertEqual(
            read_initial_admin_password(str(self.config_dir)),
            "txt-secret",
        )

    def test_rejects_symlinked_credential(self):
        target = self.cache_dir / "target"
        target.write_text("secret", encoding="utf-8")
        os.symlink(target, self.cache_dir / "initial_admin_password.txt")

        with self.assertRaises(MediaStormCredentialError):
            read_initial_admin_password(str(self.config_dir))

    def test_rejects_oversized_credential(self):
        (self.cache_dir / "initial_admin_password.txt").write_bytes(
            b"x" * (MAX_INITIAL_ADMIN_PASSWORD_BYTES + 1)
        )

        with self.assertRaises(MediaStormCredentialError):
            read_initial_admin_password(str(self.config_dir))

    def test_rejects_multiline_credential(self):
        (self.cache_dir / "initial_admin_password.txt").write_text(
            "first\nsecond",
            encoding="utf-8",
        )

        with self.assertRaises(MediaStormCredentialError):
            read_initial_admin_password(str(self.config_dir))


if __name__ == "__main__":
    unittest.main()
