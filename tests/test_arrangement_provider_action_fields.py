import unittest

from utils.arrangement_action_fields import action_fields


class ProviderActionFieldsTests(unittest.TestCase):
    def fields(self, source, suffixes):
        return [
            {"key": source + "::" + suffix, "resource_id": source}
            for suffix in suffixes
        ]

    def test_nntp_excludes_account_creation_webdav_and_import_controls(self):
        source = "altmount"
        wanted = {
            "admin_username",
            "admin_password",
            "settings_token",
            "nntp",
            "nntp_source",
            "nntp_connections",
            "allow_private_nntp",
        }
        fields = self.fields(
            source,
            sorted(
                wanted
                | {
                    "create_admin",
                    "webdav_username",
                    "webdav_password",
                    "adopt_existing",
                    "adoption_preview_id",
                    "import_dir",
                    "import_strategy",
                    "restart_after_config",
                    "workflow_probe",
                }
            ),
        )
        fields += self.fields("other", ["nntp"])
        actual = action_fields(
            {"source": source, "action": "nntp", "service_key": source}, fields
        )
        self.assertEqual({source + "::" + suffix for suffix in wanted}, set(actual))

    def test_import_layout_shows_only_settings_authorization_and_output_choices(self):
        source = "altmount"
        fields = self.fields(
            source,
            [
                "settings_token",
                "admin_username",
                "admin_password",
                "import_dir",
                "import_strategy",
                "nntp",
                "nntp_source",
                "webdav_password",
                "create_admin",
                "consumer_paths",
            ],
        )
        actual = action_fields(
            {"source": source, "action": "import_layout", "service_key": source}, fields
        )
        self.assertEqual(
            {
                source + "::" + suffix
                for suffix in (
                    "settings_token",
                    "admin_username",
                    "admin_password",
                    "import_dir",
                    "import_strategy",
                )
            },
            set(actual),
        )

    def test_external_provider_card_is_exact_to_selected_protocol_and_root(self):
        source = "external_arr.movie"
        common = {
            "api_key",
            "root_folder",
            "new_root_folder",
            "quality_profile",
            "metadata_profile",
            "adopt_existing",
            "adoption_preview_id",
        }
        selected = {
            "decypharr::client_url",
            "decypharr::category",
            "decypharr::remote_path",
            "decypharr::local_path",
            "decypharr::restart_after_config",
        }
        other = {
            "altmount::client_url",
            "altmount::category",
            "altmount::enable_api",
            "providers",
            "workflow_probe",
            "indexers",
        }
        fields = self.fields(source, sorted(common | selected | other))
        actual = action_fields(
            {"source": source, "service_key": "radarr", "action": "provider.decypharr"},
            fields,
        )
        self.assertEqual(
            {source + "::" + suffix for suffix in common | selected}, set(actual)
        )

    def test_alt_account_and_webdav_do_not_display_nntp_or_import_configuration(self):
        source = "altmount"
        fields = self.fields(
            source,
            [
                "create_admin",
                "admin_username",
                "admin_password",
                "settings_token",
                "webdav_username",
                "webdav_password",
                "adopt_existing",
                "adoption_preview_id",
                "restart_after_config",
                "acknowledge_external_consumers",
                "import_dir",
                "import_strategy",
                "nntp",
                "nntp_source",
            ],
        )
        account = action_fields(
            {"source": source, "service_key": source, "action": "account"}, fields
        )
        self.assertEqual(
            {
                source + "::" + suffix
                for suffix in (
                    "create_admin",
                    "admin_username",
                    "admin_password",
                    "settings_token",
                )
            },
            set(account),
        )
        webdav = action_fields(
            {"source": source, "service_key": source, "action": "webdav"}, fields
        )
        self.assertNotIn(source + "::create_admin", webdav)
        self.assertFalse(
            any(
                key.endswith(
                    ("::nntp", "::nntp_source", "::import_dir", "::import_strategy")
                )
                for key in webdav
            )
        )
