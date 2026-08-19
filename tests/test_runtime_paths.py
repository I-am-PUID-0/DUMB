import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import runtime_paths


class RuntimePathTests(unittest.TestCase):
    def test_default_root_is_the_source_checkout(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DUMB_PROJECT_ROOT", None)
            root = runtime_paths.project_root()

        self.assertEqual(root, Path(__file__).resolve().parents[1])
        self.assertEqual(runtime_paths.pyproject_file(), str(root / "pyproject.toml"))
        self.assertEqual(
            runtime_paths.default_config_file(),
            str(root / "utils" / "dumb_config.json"),
        )

    def test_explicit_native_install_root_is_used(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"DUMB_PROJECT_ROOT": directory}, clear=False):
                root = Path(directory).resolve()
                self.assertEqual(runtime_paths.project_root(), root)
                self.assertEqual(
                    runtime_paths.healthcheck_script(), str(root / "healthcheck.py")
                )
                self.assertEqual(
                    runtime_paths.default_schema_file(),
                    str(root / "utils" / "dumb_config_schema.json"),
                )


if __name__ == "__main__":
    unittest.main()
