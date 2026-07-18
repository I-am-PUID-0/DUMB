import subprocess
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

    def test_extraction_classifies_enosys_as_runtime_or_storage_failure(self):
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
            ):
                with self.assertRaises(RuntimeError) as raised:
                    installer._extract_archive(str(archive_path))

            message = str(raised.exception)
            self.assertIn("Container filesystem operations returned ENOSYS", message)
            self.assertIn("container runtime/seccomp profile", message)
            self.assertIn("rather than re-downloading the archive", message)

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
