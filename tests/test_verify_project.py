import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts import verify_project


class VerifyProjectTests(unittest.TestCase):
    def setUp(self):
        self.original_root = verify_project.ROOT

    def tearDown(self):
        verify_project.ROOT = self.original_root

    def _write_release_fixture(self, root, source, released):
        source_line = "" if source is None else f"version = {json.dumps(source)}\n"
        root.joinpath("pyproject.toml").write_text(
            f"[project]\n{source_line}", encoding="utf-8"
        )
        root.joinpath(".github").mkdir()
        root.joinpath(".github", ".release-please-manifest.json").write_text(
            json.dumps({".": released}), encoding="utf-8"
        )
        verify_project.ROOT = root

    def test_release_baseline_accepts_equal_and_newer_source_candidates(self):
        for source, released in (
            ("2.23.0", "2.23.0"),
            ("2.23.1", "2.23.0"),
            ("2.24.0", "2.23.0"),
            ("3.0.0", "2.23.0"),
            ("2.10.0", "2.9.9"),
            ("2.23.10", "2.23.9"),
        ):
            with self.subTest(source=source, released=released):
                with tempfile.TemporaryDirectory() as directory:
                    self._write_release_fixture(Path(directory), source, released)
                    verify_project.check_release_manifest()

    def test_release_baseline_rejects_source_downgrades_with_recovery_guidance(self):
        for source, released in (
            ("2.22.9", "2.23.0"),
            ("2.23.0", "2.23.1"),
            ("2.9.9", "2.10.0"),
            ("2.99.99", "3.0.0"),
        ):
            with self.subTest(source=source, released=released):
                with tempfile.TemporaryDirectory() as directory:
                    self._write_release_fixture(Path(directory), source, released)
                    with io.StringIO() as stderr, contextlib.redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as error:
                            verify_project.check_release_manifest()
                        self.assertIn("advance the source candidate", stderr.getvalue())
                        self.assertIn(
                            "preserve the actual release baseline", stderr.getvalue()
                        )
                    self.assertEqual(error.exception.code, 1)

    def test_release_baseline_requires_plain_stable_semver_in_both_files(self):
        invalid = (
            None,
            True,
            2.24,
            "",
            "2.24",
            "v2.24.0",
            "2.24.0-dev.1",
            "2.24.0+build.1",
            "02.24.0",
            "2.024.0",
            "2.24.00",
            " 2.24.0",
            "2.24.0\n",
        )
        for location in ("source", "released"):
            for value in invalid:
                with self.subTest(location=location, value=value):
                    with tempfile.TemporaryDirectory() as directory:
                        self._write_release_fixture(
                            Path(directory),
                            value if location == "source" else "2.24.0",
                            value if location == "released" else "2.23.0",
                        )
                        with (
                            io.StringIO() as stderr,
                            contextlib.redirect_stderr(stderr),
                        ):
                            with self.assertRaises(SystemExit) as error:
                                verify_project.check_release_manifest()
                            self.assertIn(
                                (
                                    "pyproject.toml"
                                    if location == "source"
                                    else ".release-please-manifest.json"
                                ),
                                stderr.getvalue(),
                            )
                            self.assertIn("plain stable SemVer", stderr.getvalue())
                            self.assertIn("DUMB_VERSION", stderr.getvalue())
                        self.assertEqual(error.exception.code, 1)

    def test_repository_metadata_accepts_current_release_baseline(self):
        verify_project.check_release_manifest()

    def test_dockerignore_check_accepts_required_patterns_with_comments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            verify_project.ROOT = root
            root.joinpath(".dockerignore").write_text(
                "# generated files\n"
                ".git\n"
                ".github\n"
                ".env\n"
                "config/\n"
                "log/\n"
                "logs/\n"
                "__pycache__/\n"
                "*.py[cod]\n"
                ".ruff_cache/\n"
                ".venv/\n"
                "venv/\n",
                encoding="utf-8",
            )

            verify_project.check_dockerignore_required_patterns()

    def test_dockerignore_check_reports_missing_required_patterns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            verify_project.ROOT = root
            root.joinpath(".dockerignore").write_text(".git\n.env\n", encoding="utf-8")

            with io.StringIO() as stderr, contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    verify_project.check_dockerignore_required_patterns()

        self.assertEqual(ctx.exception.code, 1)

    def test_env_example_check_accepts_generated_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            verify_project.ROOT = root
            self._write_env_generator_fixture(root, generated="PUID=1000\n")
            root.joinpath(".env.example").write_text("PUID=1000\n", encoding="utf-8")

            verify_project.check_env_example()

    def test_env_example_check_reports_stale_generated_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            verify_project.ROOT = root
            self._write_env_generator_fixture(root, generated="PUID=1000\n")
            root.joinpath(".env.example").write_text("PUID=1001\n", encoding="utf-8")

            with io.StringIO() as stderr, contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    verify_project.check_env_example()

        self.assertEqual(ctx.exception.code, 1)

    def _write_env_generator_fixture(self, root, generated):
        root.joinpath("scripts").mkdir()
        root.joinpath("utils").mkdir()
        root.joinpath("utils", "dumb_config.json").write_text(
            '{"puid": 1000}', encoding="utf-8"
        )
        root.joinpath("scripts", "generate_env_example.py").write_text(
            "from pathlib import Path\n"
            "ROOT = Path(__file__).resolve().parents[1]\n"
            "CONFIG_PATH = ROOT / 'utils' / 'dumb_config.json'\n"
            "ENV_EXAMPLE_PATH = ROOT / '.env.example'\n"
            "def generate_env_example(config):\n"
            f"    return {generated!r}\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
