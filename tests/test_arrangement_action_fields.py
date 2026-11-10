import unittest
from utils.arrangement_action_fields import action_fields


class ActionFieldScopeTests(unittest.TestCase):
    def test_bazarr_leaf_keeps_native_language_choices_without_other_arr_profiles(self):
        fields = [
            {"key": "bazarr::" + suffix, "resource_id": "bazarr"}
            for suffix in (
                "language_profile",
                "languages",
                "enable_integrations",
                "api_key",
                "radarr.HD::quality_profile",
                "sonarr.TV::quality_profile",
            )
        ]
        selected = action_fields(
            {
                "source": "bazarr",
                "service_key": "bazarr",
                "action": "arr_connection",
                "target": "radarr.HD",
            },
            fields,
        )
        self.assertIn("bazarr::language_profile", selected)
        self.assertIn("bazarr::languages", selected)
        self.assertIn("bazarr::radarr.HD::quality_profile", selected)
        self.assertNotIn("bazarr::sonarr.TV::quality_profile", selected)
        self.assertNotIn(
            "bazarr::languages",
            action_fields(
                {
                    "source": "bazarr",
                    "service_key": "bazarr",
                    "action": "arr_connections",
                },
                fields,
            ),
        )
