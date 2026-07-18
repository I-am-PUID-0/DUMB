import io
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from utils import arr


class ArrInstallerArchiveTests(unittest.TestCase):
    def _installer_and_archive(self, temp_dir):
        archive_path = Path(temp_dir) / "Prowlarr.test.linux-core-x64.tar.gz"
        archive_path.write_bytes(b"not-a-complete-archive")
        installer = arr.ArrInstaller("prowlarr", install_dir=temp_dir)
        return installer, archive_path

    def test_validation_captures_tar_stderr_in_structured_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            installer, archive_path = self._installer_and_archive(temp_dir)
            result = subprocess.CompletedProcess(
                args=["tar"],
                returncode=2,
                stderr=(
                    "gzip: stdin: unexpected end of file\n"
                    "tar: Child returned status 1"
                ),
            )

            with (
                patch.object(arr.subprocess, "run", return_value=result) as run,
                patch.object(
                    arr.shutil,
                    "disk_usage",
                    return_value=SimpleNamespace(free=512 * 1024 * 1024),
                ),
            ):
                with self.assertRaises(RuntimeError) as raised:
                    installer._validate_archive(str(archive_path))

            message = str(raised.exception)
            self.assertIn("archive validation failed", message)
            self.assertIn("tar exit 2", message)
            self.assertIn("gzip: stdin: unexpected end of file", message)
            self.assertIn("tar: Child returned status 1", message)
            self.assertIn("free space 512.0 MiB", message)
            self.assertEqual(run.call_args.args[0], ["tar", "tzf", str(archive_path)])

    def test_extraction_captures_no_space_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            installer, archive_path = self._installer_and_archive(temp_dir)
            result = subprocess.CompletedProcess(
                args=["tar"],
                returncode=2,
                stderr="tar: Prowlarr/Prowlarr: Cannot write: No space left on device",
            )

            with (
                patch.object(arr.subprocess, "run", return_value=result),
                patch.object(
                    arr.shutil,
                    "disk_usage",
                    return_value=SimpleNamespace(free=0),
                ),
            ):
                with self.assertRaises(RuntimeError) as raised:
                    installer._extract_archive(str(archive_path))

            message = str(raised.exception)
            self.assertIn("archive extraction failed", message)
            self.assertIn("No space left on device", message)
            self.assertIn("free space 0.0 B [0 bytes]", message)

    def test_extraction_retries_enosys_with_security_filtered_python(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            installer, archive_path = self._installer_and_archive(temp_dir)
            result = subprocess.CompletedProcess(
                args=["tar"],
                returncode=2,
                stderr="tar: Prowlarr/de-DE: Cannot mkdir: Function not implemented",
            )

            with (
                patch.object(arr.subprocess, "run", return_value=result),
                patch.object(
                    arr.shutil,
                    "disk_usage",
                    return_value=SimpleNamespace(free=512 * 1024 * 1024),
                ),
                patch.object(installer, "_extract_archive_with_python") as fallback,
            ):
                installer._extract_archive(str(archive_path))

            fallback.assert_called_once_with(str(archive_path))

    def test_enosys_error_preserves_tar_and_fallback_failures(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            installer, archive_path = self._installer_and_archive(temp_dir)
            result = subprocess.CompletedProcess(
                args=["tar"],
                returncode=2,
                stderr="tar: Prowlarr/de-DE: Cannot mkdir: Function not implemented",
            )

            with (
                patch.object(arr.subprocess, "run", return_value=result),
                patch.object(
                    installer,
                    "_extract_archive_with_python",
                    side_effect=tarfile.ReadError("invalid gzip archive"),
                ),
            ):
                with self.assertRaises(RuntimeError) as raised:
                    installer._extract_archive(str(archive_path))

            message = str(raised.exception)
            self.assertIn("Ubuntu 26.04's security-hardened tar uses openat2", message)
            self.assertIn("Security-filtered Python extraction also failed", message)
            self.assertIn("invalid gzip archive", message)

    def test_python_fallback_rejects_archive_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir) / "install"
            install_dir.mkdir()
            archive_path = install_dir / "unsafe.tar.gz"
            installer = arr.ArrInstaller("prowlarr", install_dir=str(install_dir))
            payload = b"must stay inside extraction root"
            member = tarfile.TarInfo("../outside.txt")
            member.size = len(payload)
            with tarfile.open(archive_path, mode="w:gz") as archive:
                archive.addfile(member, io.BytesIO(payload))

            with self.assertRaises(tarfile.FilterError):
                installer._extract_archive_with_python(str(archive_path))

            self.assertFalse((Path(temp_dir) / "outside.txt").exists())

    def test_python_fallback_extracts_safe_executable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "safe.tar.gz"
            installer = arr.ArrInstaller("prowlarr", install_dir=temp_dir)
            payload = b"prowlarr binary"
            member = tarfile.TarInfo("Prowlarr/Prowlarr")
            member.mode = 0o755
            member.size = len(payload)
            with tarfile.open(archive_path, mode="w:gz") as archive:
                archive.addfile(member, io.BytesIO(payload))

            installer._extract_archive_with_python(str(archive_path))

            binary_path = Path(temp_dir) / "Prowlarr" / "Prowlarr"
            self.assertEqual(binary_path.read_bytes(), payload)
            self.assertTrue(binary_path.stat().st_mode & 0o100)

    def test_archive_is_validated_and_storage_logged_before_extraction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            installer, archive_path = self._installer_and_archive(temp_dir)
            events = []

            with (
                patch.object(
                    installer,
                    "_validate_archive",
                    side_effect=lambda _path: events.append("validate"),
                ),
                patch.object(
                    installer,
                    "_log_archive_storage",
                    side_effect=lambda _path: events.append("storage"),
                ),
                patch.object(
                    installer,
                    "_extract_archive",
                    side_effect=lambda _path: events.append("extract"),
                ),
            ):
                installer._validate_and_extract_archive(str(archive_path))

            self.assertEqual(events, ["validate", "storage", "extract"])


if __name__ == "__main__":
    unittest.main()
