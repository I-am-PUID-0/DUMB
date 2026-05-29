import sys
import types
import unittest


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def _install_runtime_stubs():
    global_logger = types.ModuleType("utils.global_logger")
    global_logger.logger = _Logger()
    sys.modules["utils.global_logger"] = global_logger

    config_loader = types.ModuleType("utils.config_loader")
    config_loader.CONFIG_MANAGER = types.SimpleNamespace(get=lambda *args, **kwargs: None)
    sys.modules["utils.config_loader"] = config_loader

    core_services = types.ModuleType("utils.core_services")
    core_services.get_core_services = lambda _config: []
    sys.modules["utils.core_services"] = core_services

    user_management = types.ModuleType("utils.user_management")
    user_management.chown_recursive = lambda *args, **kwargs: None
    sys.modules["utils.user_management"] = user_management


_install_runtime_stubs()

from utils import prowlarr_settings


ZILEAN_DEFINITION = (
    "id: zilean name: Zilean caps: categories: Movies: Movies TV: TV "
    "search: paths: - path: /dmm/filtered method: post inputs: "
    "Episode: \"{{ if .Query.Ep }}{{ .Query.Ep }}{{ else }}{{ end }}\" "
    "filters: - name: replace args: [\"movie\", \"Movies\"]"
)

STREMTHRU_DEFINITION = (
    "id: stremthru name: StremThru caps: categories: Movies: Movies TV: TV "
    "search: paths: - path: /v0/torznab categories: [Movies] "
    "- path: /v0/torznab categories: [TV]"
)


class ProwlarrWhisparrCategoryTests(unittest.TestCase):
    def test_whisparr_sync_categories_are_adult_only(self):
        self.assertNotIn(2000, prowlarr_settings.WHISPARR_SYNC_CATEGORIES)
        self.assertNotIn(5000, prowlarr_settings.WHISPARR_SYNC_CATEGORIES)
        self.assertIn(6000, prowlarr_settings.WHISPARR_SYNC_CATEGORIES)

    def test_zilean_definition_gets_xxx_caps_and_query_category(self):
        patched = prowlarr_settings._ensure_custom_indexer_whisparr_caps(
            "zilean.yml", ZILEAN_DEFINITION
        )

        self.assertIn("XXX: XXX", patched)
        self.assertIn("Category:", patched)
        self.assertIn("join .Categories", patched)
        self.assertIn("args: [\"xxx\", \"XXX\"]", patched)

    def test_stremthru_definition_gets_xxx_caps_and_paths(self):
        patched = prowlarr_settings._ensure_custom_indexer_whisparr_caps(
            "stremthru.yml", STREMTHRU_DEFINITION
        )

        self.assertIn("XXX: XXX", patched)
        self.assertIn("categories: [Movies, XXX]", patched)
        self.assertIn("categories: [TV, XXX]", patched)


if __name__ == "__main__":
    unittest.main()
