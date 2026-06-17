import unittest

from utils.core_services import (
    get_core_services,
    has_core_service,
    normalize_core_services,
)
from utils.dependency_map import (
    build_conditional_dependency_map,
    filter_conditional_deps_for_instance,
)


class CoreServiceNormalizationTests(unittest.TestCase):
    def test_normalize_core_services_accepts_strings_and_sequences(self):
        self.assertEqual(
            normalize_core_services([" Decypharr ", "nzbdav, zurg", None, 42]),
            ["decypharr", "nzbdav", "zurg"],
        )
        self.assertEqual(
            normalize_core_services("decypharr, nzbdav"), ["decypharr", "nzbdav"]
        )

    def test_get_core_services_merges_and_deduplicates_legacy_and_plural_fields(self):
        config = {
            "core_service": "Decypharr",
            "core_services": ["decypharr", "NzbDAV, zurg"],
        }

        self.assertEqual(get_core_services(config), ["decypharr", "nzbdav", "zurg"])
        self.assertTrue(has_core_service(config, "NZBDAV"))
        self.assertFalse(has_core_service(config, "riven"))


class ConditionalDependencyMapTests(unittest.TestCase):
    def _build(self, config):
        return build_conditional_dependency_map(lambda key: config.get(key, {}))

    def test_media_and_arr_dependencies_are_added_only_for_enabled_instances(self):
        deps = self._build(
            {
                "plex": {"enabled": True},
                "jellyfin": {"enabled": False},
                "sonarr": {"instances": {"main": {"enabled": True}}},
                "radarr": {"instances": {"movies": {"enabled": False}}},
                "whisparr": {"instances": {"adult": {"enabled": True}}},
            }
        )

        self.assertEqual(deps["tautulli"], {"plex"})
        self.assertEqual(deps["seerr"], {"plex"})
        self.assertEqual(deps["prowlarr"], {"sonarr", "whisparr"})
        self.assertEqual(deps["profilarr"], {"sonarr", "whisparr"})

    def test_neutarr_depends_only_on_arr_instances_with_use_neutarr(self):
        deps = self._build(
            {
                "sonarr": {
                    "instances": {"main": {"enabled": True, "use_neutarr": True}}
                },
                "radarr": {
                    "instances": {"movies": {"enabled": True, "use_neutarr": False}}
                },
                "whisparr": {
                    "instances": {"adult": {"enabled": False, "use_neutarr": True}}
                },
            }
        )

        self.assertEqual(deps["neutarr"], {"sonarr"})

    def test_arr_postgres_adds_postgres_dependency_only_when_explicitly_enabled(self):
        deps = self._build(
            {
                "sonarr": {
                    "instances": {"main": {"enabled": True, "postgres_enabled": True}}
                },
                "radarr": {
                    "instances": {
                        "movies": {"enabled": True, "postgres_enabled": False},
                        "legacy": {"enabled": True},
                    }
                },
                "lidarr": {
                    "instances": {"music": {"enabled": True, "postgres_enabled": True}}
                },
                "prowlarr": {
                    "instances": {
                        "indexers": {"enabled": True, "postgres_enabled": True}
                    }
                },
                "whisparr": {
                    "instances": {"adult": {"enabled": True, "postgres_enabled": True}}
                },
            }
        )

        self.assertIn("postgres", deps["sonarr"])
        self.assertIn("postgres", deps["lidarr"])
        self.assertIn("postgres", deps["prowlarr"])
        self.assertIn("postgres", deps["whisparr"])
        self.assertNotIn("postgres", deps.get("radarr", set()))

    def test_rclone_dependencies_include_provider_flags_and_core_service_links(self):
        deps = self._build(
            {
                "rclone": {
                    "instances": {
                        "zurg": {"enabled": True, "zurg_enabled": True},
                        "decypharr": {"enabled": True, "decypharr_enabled": True},
                        "usenet": {"enabled": True, "core_service": "nzbdav"},
                        "disabled": {"enabled": False, "key_type": "nzbdav"},
                    }
                }
            }
        )

        self.assertEqual(deps["rclone"], {"zurg", "decypharr", "nzbdav"})

    def test_filter_conditional_deps_for_rclone_instance_replaces_aggregate_union(self):
        aggregate = {"rclone": {"zurg", "decypharr", "nzbdav"}, "prowlarr": {"sonarr"}}

        filtered = filter_conditional_deps_for_instance(
            aggregate,
            "rclone",
            {"enabled": True, "key_type": "nzbdav"},
        )

        self.assertEqual(filtered["rclone"], {"nzbdav"})
        self.assertEqual(filtered["prowlarr"], {"sonarr"})

        no_provider = filter_conditional_deps_for_instance(
            aggregate, "rclone", {"enabled": True}
        )
        self.assertNotIn("rclone", no_provider)


if __name__ == "__main__":
    unittest.main()
