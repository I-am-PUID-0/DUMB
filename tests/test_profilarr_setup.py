import importlib
import sys
import tempfile
import unittest
from pathlib import Path

utils_pkg = sys.modules.get("utils")
for module_name in (
    "utils.profilarr_settings",
    "utils.versions",
    "utils.core_services",
    "utils.config_loader",
    "utils.decypharr_settings",
    "utils.user_management",
):
    sys.modules.pop(module_name, None)
    attr_name = module_name.rsplit(".", 1)[-1]
    if utils_pkg is not None and hasattr(utils_pkg, attr_name):
        delattr(utils_pkg, attr_name)
profilarr_settings = importlib.import_module("utils.profilarr_settings")

from utils.versions import PROFILARR_LEGACY_RELEASE_VERSION, Versions

versions = Versions()
validate_profilarr_legacy_layout = profilarr_settings.validate_profilarr_legacy_layout


class ProfilarrSetupTests(unittest.TestCase):
    def test_latest_official_release_resolves_to_legacy_compatible_version(self):
        release, version_to_write = versions.resolve_profilarr_release_version(
            {
                "repo_owner": "Dictionarry-Hub",
                "repo_name": "profilarr",
                "release_version": "latest",
            }
        )

        self.assertEqual(PROFILARR_LEGACY_RELEASE_VERSION, release)
        self.assertEqual(PROFILARR_LEGACY_RELEASE_VERSION, version_to_write)

    def test_explicit_release_version_is_preserved(self):
        release, version_to_write = versions.resolve_profilarr_release_version(
            {
                "repo_owner": "Dictionarry-Hub",
                "repo_name": "profilarr",
                "release_version": "v2.0.7",
            }
        )

        self.assertEqual("v2.0.7", release)
        self.assertEqual("v2.0.7", version_to_write)

    def test_legacy_layout_validation_requires_backend_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend_dir = Path(tmpdir) / "backend"
            success, error = validate_profilarr_legacy_layout(
                "Profiles", str(backend_dir)
            )

            self.assertFalse(success)
            self.assertIn("legacy backend/frontend layout", error)

            entrypoint = backend_dir / "app" / "main.py"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("def create_app(): pass\n")

            success, error = validate_profilarr_legacy_layout(
                "Profiles", str(backend_dir)
            )
            self.assertTrue(success)
            self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
