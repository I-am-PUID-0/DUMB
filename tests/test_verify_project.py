import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from scripts import verify_project


class VerifyProjectTests(unittest.TestCase):
    def setUp(self):
        self.original_root = verify_project.ROOT

    def tearDown(self):
        verify_project.ROOT = self.original_root

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
