import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_stubs():
    global_logger = types.ModuleType("utils.global_logger")
    global_logger.logger = types.SimpleNamespace()
    sys.modules["utils.global_logger"] = global_logger

    download = types.ModuleType("utils.download")
    download.Downloader = lambda: types.SimpleNamespace()
    sys.modules["utils.download"] = download

    config_loader = types.ModuleType("utils.config_loader")
    config_loader.CONFIG_MANAGER = types.SimpleNamespace(get=lambda *args, **kwargs: {})
    sys.modules["utils.config_loader"] = config_loader


_install_stubs()
sys.modules.pop("utils.versions", None)
versions = importlib.import_module("utils.versions")


class PgAdminVersionParserTests(unittest.TestCase):
    def test_parse_python_literal_assignments_reads_literals_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir, "version.py")
            marker = Path(temp_dir, "marker")
            path.write_text(
                "APP_RELEASE = 9\n"
                "APP_REVISION = 1\n"
                "APP_SUFFIX = 'beta'\n"
                f"UNSAFE = open({str(marker)!r}, 'w').write('executed')\n",
                encoding="utf-8",
            )

            parsed = versions._parse_python_literal_assignments(path)

        self.assertEqual(
            parsed,
            {"APP_RELEASE": 9, "APP_REVISION": 1, "APP_SUFFIX": "beta"},
        )
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
