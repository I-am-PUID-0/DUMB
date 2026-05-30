import tempfile
import unittest
from pathlib import Path

from utils.project_metadata import get_project_version


class ProjectMetadataTests(unittest.TestCase):
    def test_reads_project_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pyproject.toml"
            path.write_text(
                '[project]\nname = "DUMB"\nversion = "2.6.0"\n',
                encoding="utf-8",
            )

            self.assertEqual(get_project_version(str(path)), "2.6.0")

    def test_falls_back_to_legacy_poetry_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pyproject.toml"
            path.write_text(
                '[tool.poetry]\nname = "DUMB"\nversion = "2.5.0"\n',
                encoding="utf-8",
            )

            self.assertEqual(get_project_version(str(path)), "2.5.0")

    def test_returns_default_for_missing_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pyproject.toml"
            path.write_text('[project]\nname = "DUMB"\n', encoding="utf-8")

            self.assertEqual(
                get_project_version(str(path), default="unknown"), "unknown"
            )


if __name__ == "__main__":
    unittest.main()
