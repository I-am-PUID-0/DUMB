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
    config_loader.CONFIG_MANAGER = types.SimpleNamespace(
        get=lambda *args, **kwargs: None
    )
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
    'Episode: "{{ if .Query.Ep }}{{ .Query.Ep }}{{ else }}{{ end }}" '
    'filters: - name: replace args: ["movie", "Movies"]'
)

STREMTHRU_DEFINITION = (
    "id: stremthru name: StremThru caps: categories: Movies: Movies TV: TV "
    "search: paths: - path: /v0/torznab categories: [Movies] "
    "- path: /v0/torznab categories: [TV]"
)


APPLICATION_SCHEMA = {
    "implementation": "Whisparr",
    "implementationName": "Whisparr",
    "configContract": "WhisparrSettings",
    "infoLink": "https://wiki.servarr.com/prowlarr/supported#whisparr",
    "fields": [
        {"name": "baseUrl", "value": "http://old:6969"},
        {"name": "apiKey", "value": "old-key"},
        {"name": "syncCategories", "value": [2000, 5000]},
        {"name": "syncLevel", "value": "addOnly"},
    ],
}

INDEXER_SCHEMA = {
    "implementation": "Cardigann",
    "implementationName": "Cardigann",
    "configContract": "CardigannSettings",
    "infoLink": "https://example.invalid/indexer",
    "protocol": "torrent",
    "definitionName": "zilean",
    "fields": [
        {"name": "baseUrl", "value": "https://old.invalid"},
        {"name": "definitionFile", "value": "Custom/old"},
        {"name": "apiUrl", "value": "https://old.invalid/api"},
    ],
}


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
        self.assertIn('args: ["xxx", "XXX"]', patched)

    def test_stremthru_definition_gets_xxx_caps_and_paths(self):
        patched = prowlarr_settings._ensure_custom_indexer_whisparr_caps(
            "stremthru.yml", STREMTHRU_DEFINITION
        )

        self.assertIn("XXX: XXX", patched)
        self.assertIn("categories: [Movies, XXX]", patched)
        self.assertIn("categories: [TV, XXX]", patched)


class ProwlarrPayloadHelperTests(unittest.TestCase):
    def test_build_fields_from_schema_overrides_existing_names_case_insensitively(self):
        fields = prowlarr_settings._build_fields_from_schema(
            {
                "fields": [
                    {"name": "baseUrl", "value": "http://old"},
                    {"name": "ApiKey", "value": "old-key"},
                    {"name": "syncCategories", "value": [2000]},
                ]
            },
            {
                "BASEURL": "http://new",
                "apiKey": "new-key",
                "syncCategories": [6000],
                "unknown": "ignored",
            },
        )

        self.assertEqual(
            {field["name"]: field["value"] for field in fields},
            {
                "baseUrl": "http://new",
                "ApiKey": "new-key",
                "syncCategories": [6000],
            },
        )

    def test_build_whisparr_application_payload_sets_adult_categories(self):
        payload = prowlarr_settings._build_application_payload(
            APPLICATION_SCHEMA,
            "Whisparr",
            "http://127.0.0.1:6969",
            "arr-key",
            "default",
            tag_ids=[42],
        )
        fields = {field["name"]: field["value"] for field in payload["fields"]}

        self.assertEqual(payload["name"], "Whisparr (default)")
        self.assertEqual(payload["syncLevel"], "fullSync")
        self.assertEqual(payload["tags"], [42])
        self.assertEqual(fields["baseUrl"], "http://127.0.0.1:6969")
        self.assertEqual(fields["apiKey"], "arr-key")
        self.assertEqual(
            fields["syncCategories"], prowlarr_settings.WHISPARR_SYNC_CATEGORIES
        )

    def test_build_non_whisparr_application_payload_does_not_override_categories(self):
        schema = {
            **APPLICATION_SCHEMA,
            "implementation": "Radarr",
            "implementationName": "Radarr",
        }
        payload = prowlarr_settings._build_application_payload(
            schema,
            "Radarr",
            "http://127.0.0.1:7878",
            "arr-key",
            "movies",
        )
        fields = {field["name"]: field["value"] for field in payload["fields"]}

        self.assertEqual(fields["syncCategories"], [2000, 5000])

    def test_is_application_current_allows_extra_user_tags_but_requires_managed_tags(
        self,
    ):
        desired = {
            "enable": True,
            "syncLevel": "fullSync",
            "implementation": "Whisparr",
            "configContract": "WhisparrSettings",
            "tags": [42],
            "fields": [{"name": "baseUrl", "value": "http://127.0.0.1:6969"}],
        }
        existing = {
            **desired,
            "tags": [7, 42],
            "fields": [
                {"name": "baseUrl", "value": "http://127.0.0.1:6969"},
                {"name": "extraUserField", "value": "left-alone"},
            ],
        }

        self.assertTrue(prowlarr_settings._is_application_current(existing, desired))

        existing_missing_tag = {**existing, "tags": [7]}
        self.assertFalse(
            prowlarr_settings._is_application_current(existing_missing_tag, desired)
        )

    def test_find_existing_application_matches_by_name_or_base_url(self):
        desired = {"name": "Whisparr (default)"}
        schema = {"implementation": "Whisparr"}
        apps = [
            {
                "name": "Different name",
                "implementation": "Whisparr",
                "fields": [{"name": "baseUrl", "value": "http://127.0.0.1:6969/"}],
            }
        ]

        self.assertIs(
            prowlarr_settings._find_existing_application(
                apps, desired, schema, "http://127.0.0.1:6969"
            ),
            apps[0],
        )

    def test_build_indexer_payload_sets_custom_definition_fields(self):
        payload = prowlarr_settings._build_indexer_payload(
            INDEXER_SCHEMA,
            "Zilean",
            "http://127.0.0.1:8182",
            tag_ids=[12],
        )
        fields = {field["name"]: field["value"] for field in payload["fields"]}

        self.assertEqual(payload["name"], "Zilean")
        self.assertEqual(payload["protocol"], "torrent")
        self.assertEqual(payload["definitionFile"], "Custom/zilean")
        self.assertEqual(payload["tags"], [12])
        self.assertEqual(fields["baseUrl"], "http://127.0.0.1:8182")
        self.assertEqual(fields["definitionFile"], "Custom/zilean")
        self.assertEqual(fields["apiUrl"], "http://127.0.0.1:8182")


if __name__ == "__main__":
    unittest.main()
