import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from utils import artifact_verification as verification


class PublisherSignatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("gpg") or not shutil.which("gpgv"):
            raise RuntimeError("Signature regression tests require gpg and gpgv")
        cls.directory = tempfile.TemporaryDirectory(prefix="dumb-signature-test-")
        cls.root = Path(cls.directory.name)
        cls.home = cls.root / "gnupg"
        cls.home.mkdir(mode=0o700)
        cls.gpg = ["gpg", "--homedir", str(cls.home), "--batch", "--yes"]
        try:
            subprocess.run(
                cls.gpg
                + [
                    "--pinentry-mode",
                    "loopback",
                    "--passphrase",
                    "",
                    "--quick-generate-key",
                    "Artifact Regression <artifact@example.invalid>",
                    "ed25519",
                    "sign",
                    "0",
                ],
                capture_output=True,
                check=True,
                timeout=30,
            )
            result = subprocess.run(
                cls.gpg + ["--with-colons", "--list-keys"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            cls.fingerprint = next(
                line.split(":")[9]
                for line in result.stdout.splitlines()
                if line.startswith("fpr:")
            )
            cls.key = cls.root / "publisher.asc"
            subprocess.run(
                cls.gpg + ["--armor", "--output", str(cls.key), "--export"],
                capture_output=True,
                check=True,
                timeout=30,
            )
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        subprocess.run(
            ["gpgconf", "--homedir", str(cls.home), "--kill", "gpg-agent"],
            capture_output=True,
            check=True,
            timeout=30,
        )
        cls.directory.cleanup()

    def setUp(self):
        self.artifact = self.root / "artifact.txt"
        self.artifact.write_bytes(b"a verified artifact\n")
        self.signature = self.root / "artifact.sig"
        self.keys = patch.dict(
            verification._PUBLISHERS,
            {"fixture": (self.key, self.fingerprint)},
        )
        self.keys.start()
        self.addCleanup(self.keys.stop)

    def sign(self, clear=False, digest="SHA256"):
        subprocess.run(
            self.gpg
            + [
                "--digest-algo",
                digest,
                "--armor",
                "--output",
                str(self.signature),
                "--clearsign" if clear else "--detach-sign",
                str(self.artifact),
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )

    def test_real_detached_signature_accepts_only_unchanged_artifact(self):
        self.sign()
        verification.verify_publisher_signature(
            self.artifact, self.signature, "fixture"
        )
        self.artifact.write_bytes(b"tampered artifact\n")
        with self.assertRaises(verification.ArtifactVerificationError):
            verification.verify_publisher_signature(
                self.artifact, self.signature, "fixture"
            )

    def test_real_clear_signature_requires_identical_downloaded_manifest(self):
        self.sign(clear=True)
        verification.verify_publisher_signature(
            self.artifact, self.signature, "fixture"
        )
        self.artifact.write_bytes(b"different unsigned manifest\n")
        with self.assertRaisesRegex(
            verification.ArtifactVerificationError, "does not match"
        ):
            verification.verify_publisher_signature(
                self.artifact, self.signature, "fixture"
            )

    def test_clear_signature_normalizes_only_line_endings(self):
        self.sign(clear=True)
        self.artifact.write_bytes(b"a verified artifact\r\n\r\n")
        verification.verify_publisher_signature(
            self.artifact, self.signature, "fixture"
        )
        self.artifact.write_bytes(b"a verified artifact \r\n")
        with self.assertRaises(verification.ArtifactVerificationError):
            verification.verify_publisher_signature(
                self.artifact, self.signature, "fixture"
            )

    def test_unreviewed_publisher_and_wrong_primary_fingerprint_fail(self):
        self.sign()
        with self.assertRaises(verification.ArtifactVerificationError):
            verification.verify_publisher_signature(
                self.artifact, self.signature, "unreviewed"
            )
        with patch.dict(verification._PUBLISHERS, {"fixture": (self.key, "0" * 40)}):
            with self.assertRaises(verification.ArtifactVerificationError):
                verification.verify_publisher_signature(
                    self.artifact, self.signature, "fixture"
                )

    def test_signature_damage_and_missing_signature_fail(self):
        self.sign()
        self.signature.write_bytes(b"unsigned data")
        with self.assertRaises(verification.ArtifactVerificationError):
            verification.verify_publisher_signature(
                self.artifact, self.signature, "fixture"
            )
        self.signature.unlink()
        with self.assertRaises(verification.ArtifactVerificationError):
            verification.verify_publisher_signature(
                self.artifact, self.signature, "fixture"
            )

    def test_other_publishers_key_cannot_authenticate_artifact(self):
        self.sign()
        with self.assertRaises(verification.ArtifactVerificationError):
            verification.verify_publisher_signature(
                self.artifact, self.signature, "dotnet"
            )

    def test_unprotected_temporary_root_is_refused(self):
        self.sign()
        with patch.object(
            Path, "lstat", return_value=SimpleNamespace(st_mode=0o40777, st_uid=0)
        ):
            with self.assertRaisesRegex(
                verification.ArtifactVerificationError, "staging root"
            ):
                verification.verify_publisher_signature(
                    self.artifact, self.signature, "fixture"
                )

    def test_missing_verifier_and_timeout_fail_closed(self):
        self.sign()
        with patch.object(verification.shutil, "which", return_value=None):
            with self.assertRaisesRegex(
                verification.ArtifactVerificationError, "gpgv is required"
            ):
                verification.verify_publisher_signature(
                    self.artifact, self.signature, "fixture"
                )
        with patch.object(
            verification.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("gpgv", 30),
        ):
            with self.assertRaises(verification.ArtifactVerificationError):
                verification.verify_publisher_signature(
                    self.artifact, self.signature, "fixture"
                )

    def test_real_sha1_signature_is_rejected(self):
        self.sign(digest="SHA1")
        with self.assertRaises(verification.ArtifactVerificationError):
            verification.verify_publisher_signature(
                self.artifact, self.signature, "fixture"
            )

    def test_bundled_keys_have_reviewed_fingerprints(self):
        for publisher in ("bun", "dotnet"):
            key, expected = verification._PUBLISHERS[publisher]
            result = subprocess.run(
                self.gpg + ["--with-colons", "--show-keys", str(key)],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            primary = next(
                line.split(":")[9]
                for line in result.stdout.splitlines()
                if line.startswith("fpr:")
            )
            self.assertEqual(primary, expected, publisher)


if __name__ == "__main__":
    unittest.main()
