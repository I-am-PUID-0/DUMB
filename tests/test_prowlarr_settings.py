import importlib
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def _module_available(name):
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def _install_runtime_stubs():
    import xml.etree.ElementTree as stdlib_et

    if not _module_available("defusedxml.ElementTree"):
        defusedxml = types.ModuleType("defusedxml")
        defusedxml.ElementTree = stdlib_et
        sys.modules["defusedxml"] = defusedxml
        sys.modules["defusedxml.ElementTree"] = stdlib_et

    if not _module_available("utils.global_logger"):
        global_logger = types.ModuleType("utils.global_logger")
        global_logger.logger = _Logger()
        sys.modules["utils.global_logger"] = global_logger

    if not _module_available("utils.config_loader"):
        config_loader = types.ModuleType("utils.config_loader")
        config_loader.CONFIG_MANAGER = types.SimpleNamespace(
            get=lambda *args, **kwargs: None
        )
        sys.modules["utils.config_loader"] = config_loader

    if not _module_available("utils.core_services"):
        core_services = types.ModuleType("utils.core_services")
        core_services.get_core_services = lambda _config: []
        sys.modules["utils.core_services"] = core_services

    if not _module_available("utils.user_management"):
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

ZILEAN_MULTILINE_DEFINITION = """id: zilean
name: Zilean
caps:
  categories:
    Movies: Movies
    TV: TV
search:
  paths:
    - path: /dmm/filtered
      method: get
      inputs:
        Episode: "{{ if .Query.Ep }}{{ .Query.Ep }}{{ else }}{{ end }}"
      keywordsfilters:
        - name: re_replace
          args: ["^$", "limitless"]
      fields:
        category:
          selector: category
          filters:
            - name: replace
              args: ["movie", "Movies"]
"""

STREMTHRU_DEFINITION = (
    "id: stremthru name: StremThru caps: categories: Movies: Movies TV: TV "
    "search: paths: - path: /v0/torznab categories: [Movies] "
    "- path: /v0/torznab categories: [TV]"
)

STREMTHRU_MULTILINE_DEFINITION = """id: stremthru
name: Stremthru
caps:
  categories:
    Movies: Movies
    TV: TV
search:
  paths:
    - path: /v0/torrents
      categories: [Movies]
    - path: /v0/torrents?sid={{ if .Query.IMDBID }}{{ .Query.IMDBID }}{{ else }}{{ .Config.validate_imdb_tv }}{{ end }}:{{ if .Query.Season }}{{ .Query.Season }}{{ else }}1{{ end }}:{{ if .Query.Ep }}{{ .Query.Ep }}{{ else }}1{{ end }}"
      categories: [TV]
  fields:
    category_is_tv_show:
      text: "{{ .Result.title }}"
    category:
      text: "{{ if .Result.category_is_tv_show }}TV{{ else }}Movies{{ end }}"
"""


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


TORZNAB_INDEXER_SCHEMA = {
    "implementation": "Torznab",
    "implementationName": "Generic Torznab",
    "configContract": "TorznabSettings",
    "infoLink": "https://wiki.servarr.com/prowlarr/supported-indexers#generic-torznab",
    "protocol": "torrent",
    "fields": [
        {"name": "baseUrl", "value": "https://old.invalid"},
        {"name": "apiPath", "value": "/old"},
        {"name": "apiKey", "value": "old-key"},
    ],
}


class ProwlarrWhisparrCategoryTests(unittest.TestCase):

    def test_prune_duplicate_custom_indexers_removes_legacy_elfhosted_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            duplicate = pathlib.Path(temp_dir) / "elfhosted-internal.yml"
            canonical = pathlib.Path(temp_dir) / "elfhosted-torrentio.yml"
            content = (
                "---\n"
                "id: torrentio-internal\n"
                "name: ElfHosted Customer Internal Torrentio/Knightcrawler Indexer\n"
            )
            duplicate.write_text(content)
            canonical.write_text(content)

            prowlarr_settings._prune_duplicate_custom_indexers(temp_dir)

            self.assertFalse(duplicate.exists())
            self.assertTrue(canonical.exists())

    def test_whisparr_sync_categories_include_adult_and_generic_torznab_caps(self):
        self.assertIn(6000, prowlarr_settings.WHISPARR_SYNC_CATEGORIES)
        self.assertIn(2000, prowlarr_settings.WHISPARR_SYNC_CATEGORIES)
        self.assertIn(5000, prowlarr_settings.WHISPARR_SYNC_CATEGORIES)

    def test_zilean_definition_gets_xxx_caps_without_categories_template(self):
        patched = prowlarr_settings._ensure_custom_indexer_whisparr_caps(
            "zilean.yml", ZILEAN_DEFINITION
        )

        self.assertIn("XXX: XXX", patched)
        self.assertNotIn("Category:", patched)
        self.assertNotIn(".Categories", patched)
        self.assertIn('args: ["xxx", "XXX"]', patched)

    def test_zilean_multiline_definition_keeps_category_yaml_valid(self):
        patched = prowlarr_settings._ensure_custom_indexer_whisparr_caps(
            "zilean.yml", ZILEAN_MULTILINE_DEFINITION
        )

        self.assertNotIn('end }}" Category:', patched)
        self.assertNotIn("Category:", patched)
        self.assertNotIn(".Categories", patched)
        self.assertIn("    XXX: XXX", patched)
        self.assertIn('args: ["^$", "limitless"]', patched)

    def test_stremthru_definition_uses_numeric_caps_and_paths(self):
        patched = prowlarr_settings._ensure_custom_indexer_whisparr_caps(
            "stremthru.yml", STREMTHRU_DEFINITION
        )

        self.assertIn("XXX: XXX", patched)
        self.assertIn("categories: [2000]", patched)
        self.assertIn("categories: [5000]", patched)
        self.assertNotIn("categories: [Movies, XXX]", patched)
        self.assertNotIn("categories: [TV, XXX]", patched)

    def test_stremthru_multiline_definition_reports_requested_category(self):
        patched = prowlarr_settings._ensure_custom_indexer_whisparr_caps(
            "stremthru.yml", STREMTHRU_MULTILINE_DEFINITION
        )

        self.assertIn("categorymappings:", patched)
        self.assertIn("{id: 2000, cat: Movies", patched)
        self.assertIn("{id: 5000, cat: TV", patched)
        self.assertIn("{id: 6000, cat: XXX", patched)
        self.assertIn("categories: [2000]", patched)
        self.assertIn("categories: [5000]", patched)
        self.assertNotIn("categories: [Movies, XXX]", patched)
        self.assertNotIn("categories: [TV, XXX]", patched)
        self.assertNotIn('{{ end }}"\n      categories:', patched)
        self.assertNotIn(".Categories", patched)
        self.assertIn(
            'text: "{{ if .Result.category_is_tv_show }}5000{{ else }}2000{{ end }}"',
            patched,
        )


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

    def test_ensure_stremthru_updates_existing_indexer_missing_managed_tag(self):
        schema = {
            **INDEXER_SCHEMA,
            "definitionName": "stremthru",
        }
        existing = {
            "id": 99,
            "name": "StremThru",
            "enable": True,
            "implementation": "Cardigann",
            "configContract": "CardigannSettings",
            "tags": [],
            "fields": [
                {
                    "name": "baseUrl",
                    "value": "https://stremthru.13377001.xyz/v0/torznab",
                },
                {"name": "definitionFile", "value": "Custom/stremthru"},
                {
                    "name": "apiUrl",
                    "value": "https://stremthru.13377001.xyz/v0/torznab",
                },
            ],
        }
        requests = []

        def fake_req(url, token, method="GET", body=None):
            requests.append((url, token, method, body))
            if method == "GET" and url.endswith("/api/v1/indexer"):
                return [existing]
            return {}

        with (
            mock.patch.object(
                prowlarr_settings,
                "_get_prowlarr_indexer_schemas",
                return_value=[schema],
            ),
            mock.patch.object(
                prowlarr_settings, "_get_default_app_profile_id", return_value=None
            ),
            mock.patch.object(prowlarr_settings, "_prowlarr_req", side_effect=fake_req),
        ):
            prowlarr_settings.ensure_stremthru_indexer(
                "http://127.0.0.1:9696", "token", [12]
            )

        puts = [request for request in requests if request[2] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertTrue(puts[0][0].endswith("/api/v1/indexer/99"))
        self.assertEqual(puts[0][3]["tags"], [12])

    def test_find_generic_torznab_schema_prefers_generic_torznab(self):
        self.assertIs(
            prowlarr_settings._find_generic_torznab_schema(
                [INDEXER_SCHEMA, TORZNAB_INDEXER_SCHEMA]
            ),
            TORZNAB_INDEXER_SCHEMA,
        )

    def test_build_zilean_torznab_payload_uses_native_torznab_endpoint(self):
        payload = prowlarr_settings._build_zilean_torznab_payload(
            TORZNAB_INDEXER_SCHEMA,
            "http://127.0.0.1:8182/",
            tag_ids=[12],
        )
        fields = {field["name"]: field["value"] for field in payload["fields"]}

        self.assertEqual(payload["name"], "Zilean")
        self.assertEqual(payload["implementation"], "Torznab")
        self.assertEqual(payload["configContract"], "TorznabSettings")
        self.assertEqual(payload["protocol"], "torrent")
        self.assertIsNone(payload["definitionFile"])
        self.assertEqual(payload["tags"], [12])
        self.assertEqual(fields["baseUrl"], "http://127.0.0.1:8182/torznab")
        self.assertEqual(fields["apiPath"], "/api")
        self.assertEqual(fields["apiKey"], "")

    def test_find_existing_zilean_indexer_matches_current_and_legacy_custom(self):
        current = {
            "name": "Managed Zilean",
            "fields": [{"name": "baseUrl", "value": "http://127.0.0.1:8182/torznab/"}],
        }
        legacy = {
            "name": "Legacy",
            "fields": [{"name": "definitionFile", "value": "Custom/zilean"}],
        }

        self.assertIs(
            prowlarr_settings._find_existing_zilean_indexer(
                [current], "http://127.0.0.1:8182/torznab"
            ),
            current,
        )
        self.assertIs(
            prowlarr_settings._find_existing_zilean_indexer(
                [legacy], "http://127.0.0.1:8182/torznab"
            ),
            legacy,
        )


if __name__ == "__main__":
    unittest.main()
