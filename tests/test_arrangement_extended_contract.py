import copy
import json
from pathlib import Path
import tempfile
import unittest

from utils.arrangement_plan import (
    build_plan,
    public_schema,
    public_values,
    validate_values,
)
from utils.arrangement_state import SetupCoordinator, _merge_inputs

DEFAULTS = json.loads(
    (Path(__file__).resolve().parents[1] / "utils/dumb_config.json").read_text()
)


class ExtendedSetupContractTests(unittest.TestCase):
    def test_schema_secret_flags_redact_arbitrary_native_credential_names(self):
        fields = [
            {"key": "prowlarr.Test::indexer::native::passkey", "type": "password"},
            {
                "key": "setup::rows",
                "type": "list",
                "item_fields": [{"key": "opaque", "type": "string", "secret": True}],
            },
            {
                "key": "radarr.Test::workflow_probe",
                "type": "object",
                "fields": [
                    {"key": "source_url", "type": "password"},
                    {"key": "authorize_download_import_scan", "type": "boolean"},
                ],
            },
        ]
        values = {
            fields[0]["key"]: "example-private",
            "setup::rows": [{"opaque": "example-private"}],
            "radarr.Test::workflow_probe": {
                "source_url": "https://example.invalid/private-path",
                "authorize_download_import_scan": True,
            },
        }
        safe = public_values(values, fields)
        self.assertNotIn("example-private", json.dumps(safe))
        self.assertNotIn("private-path", json.dumps(safe))
        self.assertIs(
            safe["radarr.Test::workflow_probe"]["authorize_download_import_scan"], True
        )

    def test_action_forms_scope_probes_and_exact_connection_targets(self):
        from utils.arrangement_action_fields import action_fields

        source = "seerr"
        names = [
            "api_key",
            "arr_instances",
            "workflow_probe",
            "consumer_paths",
            "radarr.HD::quality_profile",
            "radarr.UHD::quality_profile",
            "adopt_existing",
        ]
        fields = [
            {"key": source + "::" + name, "resource_id": source} for name in names
        ]
        self.assertEqual(
            action_fields({"source": source, "action": "workflow_test"}, fields),
            [source + "::workflow_probe"],
        )
        exact = action_fields(
            {"source": source, "action": "arr_connection", "target": "radarr.HD"},
            fields,
        )
        self.assertIn(source + "::radarr.HD::quality_profile", exact)
        self.assertNotIn(source + "::radarr.UHD::quality_profile", exact)
        self.assertNotIn(source + "::workflow_probe", exact)
        self.assertNotIn(source + "::consumer_paths", exact)

    def test_retained_native_discovery_preserves_secret_metadata_and_strips_defaults(
        self,
    ):
        def discover(resource, config, payload):
            if resource["id"] != "plex":
                return []
            return [
                {
                    "key": "plex::opaque",
                    "resource_id": "plex",
                    "type": "string",
                    "secret": True,
                    "default": "example-private",
                }
            ]

        with tempfile.TemporaryDirectory() as directory:
            coordinator = SetupCoordinator(
                directory,
                configuration=lambda: DEFAULTS,
                defaults=lambda: DEFAULTS,
                start_services=lambda payload: None,
                inspect_action=lambda *args: {
                    "execution": "already_present",
                    "verification": "passed",
                },
                apply_action=lambda *args: {
                    "execution": "already_present",
                    "verification": "passed",
                },
                discover_fields=discover,
                autostart=False,
            )
            self.addCleanup(coordinator.shutdown)
            plan = coordinator.plan(
                {
                    "core_services": [{"name": "plex"}],
                    "arrangement": {"values": {"plex::opaque": "example-private"}},
                }
            )
            self.assertNotIn("example-private", json.dumps(plan))
            run = coordinator.start(plan["plan_id"], "metadata-retention-test")
            coordinator.run_pending(run["run_id"])
            saved = coordinator.get_run(run["run_id"])
            native = next(
                field
                for field in saved["arrangement_schema"]["fields"]
                if field["key"] == "plex::opaque"
            )
            self.assertIs(native["secret"], True)
            self.assertNotIn("default", native)
            self.assertNotIn("example-private", json.dumps(saved))
            self.assertNotIn("example-private", json.dumps(coordinator.latest()))
            self.assertEqual(
                coordinator.store.read("inputs", plan["plan_id"])["payload"][
                    "arrangement"
                ]["values"]["plex::opaque"],
                "example-private",
            )

    def test_every_remaining_native_recommendation_is_in_the_retained_inventory(self):
        payload = {
            "core_services": [
                {"name": "lidarr", "instance_name": "Music"},
                {"name": "prowlarr", "instance_name": "Indexers"},
                {
                    "name": "cli_debrid",
                    "service_options": {"cli_debrid": {"battery_mode": "standalone"}},
                },
            ],
            "optional_services": [
                "aiostreams",
                "pgadmin",
                "bazarr",
                "zilean",
                "mediastorm",
                "cloudflared",
            ],
        }
        plan = build_plan(payload, DEFAULTS, DEFAULTS, plan_id="b" * 32)
        ids = [item["id"] for item in plan["actions"]]
        self.assertEqual(len(ids), len(set(ids)))
        for family, names in {
            "lidarr": {"workflow_test", "arr_preferences"},
            "prowlarr": {"indexers"},
            "cli_debrid": {"companion_connection"},
            "aiostreams": {"backend_presets", "public_origin"},
            "pgadmin": {"postgres_connection"},
            "bazarr": {"subtitle_providers"},
            "zilean": {"ingestion"},
            "mediastorm": {"account", "sources", "metadata"},
            "zurg": {"discovery", "library_hook"},
            "cloudflared": {"public_route"},
            "traefik": {"public_route"},
        }.items():
            with self.subTest(family=family):
                native = {
                    a["action"]
                    for a in plan["actions"]
                    if a["service_key"] == family
                    and a.get("declared_automation", a["automation"]) == "after_input"
                }
                self.assertTrue(names <= native, (names, native))
        self.assertFalse(
            any(
                a["action"] in {"providers", "population", "scan"}
                for a in plan["actions"]
            )
        )

    def test_structured_fields_validate_recursive_types_and_unknown_fields(self):
        fields = [
            {
                "key": "setup::rows",
                "type": "list",
                "max_items": 2,
                "item_fields": [
                    {"key": "id", "type": "string"},
                    {"key": "api_key", "type": "password"},
                    {
                        "key": "policy",
                        "type": "object",
                        "fields": [{"key": "enabled", "type": "boolean"}],
                    },
                ],
            }
        ]
        valid = {
            "setup::rows": [
                {"id": "one", "api_key": "example", "policy": {"enabled": False}}
            ]
        }
        self.assertEqual(validate_values(valid, fields), valid)
        for row in (
            {"id": "one", "extra": "unknown"},
            {"policy": {"enabled": "yes"}},
            {"api_key": True},
        ):
            with self.subTest(row=row), self.assertRaises(ValueError):
                validate_values({"setup::rows": [row]}, fields)

    def test_nested_native_password_defaults_are_never_public(self):
        fields = [
            {
                "key": "setup::rows",
                "type": "list",
                "item_fields": [
                    {
                        "key": "password",
                        "type": "password",
                        "default": "example-private",
                        "example": "example-private",
                    }
                ],
            }
        ]
        public = public_schema(fields)
        self.assertNotIn("example-private", json.dumps(public))
        self.assertEqual(fields[0]["item_fields"][0]["default"], "example-private")
        self.assertIs(
            public_schema(
                [
                    {
                        "key": "setup::opaque",
                        "type": "string",
                        "secret": True,
                        "default": "example-private",
                    }
                ]
            )[0]["secret"],
            True,
        )

    def test_external_credentials_follow_identity_and_endpoint(self):
        fields = [
            {
                "key": "setup::external_arrs",
                "type": "list",
                "identity_fields": ["id", "url", "service_key"],
                "item_fields": [
                    {"key": "id", "type": "string"},
                    {"key": "url", "type": "string"},
                    {"key": "service_key", "type": "string"},
                    {"key": "api_key", "type": "password"},
                ],
            }
        ]
        row = {
            "id": "remote",
            "url": "https://arr.example.invalid",
            "service_key": "radarr",
            "api_key": "example-private",
        }
        saved = {"setup::external_arrs": [row]}
        blank = {**row, "api_key": ""}
        retained = _merge_inputs(saved, {"setup::external_arrs": [blank]}, fields)
        self.assertEqual(
            retained["setup::external_arrs"][0]["api_key"], "example-private"
        )
        blank["url"] = "https://different.example.invalid"
        changed = _merge_inputs(saved, {"setup::external_arrs": [blank]}, fields)
        self.assertEqual(changed["setup::external_arrs"][0]["api_key"], "")

    def test_flat_indexer_credentials_are_not_forwarded_to_a_new_destination(self):
        base = "prowlarr.Default::indexer::private::"
        fields = [
            {"key": base + "baseUrl", "type": "string"},
            {
                "key": base + "apiKey",
                "type": "password",
                "credential_destination_key": base + "baseUrl",
            },
        ]
        saved = {
            base + "baseUrl": "https://old.example.invalid",
            base + "apiKey": "example-private",
        }
        changed = _merge_inputs(
            saved, {base + "baseUrl": "https://new.example.invalid"}, fields
        )
        self.assertNotIn(base + "apiKey", changed)
        explicit = _merge_inputs(
            saved,
            {
                base + "baseUrl": "https://new.example.invalid",
                base + "apiKey": "new-example",
            },
            fields,
        )
        self.assertEqual(explicit[base + "apiKey"], "new-example")

    def test_external_resources_have_readiness_and_no_install_action(self):
        payload = {
            "core_services": [{"name": "radarr", "instance_name": "Local"}],
            "arrangement": {
                "values": {
                    "setup::external_arrs": [
                        {
                            "id": "remote",
                            "service_key": "sonarr",
                            "url": "https://arr.example.invalid",
                            "api_key": "example-private",
                        }
                    ]
                }
            },
        }
        before = copy.deepcopy(DEFAULTS)
        plan = build_plan(payload, DEFAULTS, DEFAULTS, plan_id="a" * 32)
        external = next(
            row for row in plan["services"] if row["id"] == "external_arr.remote"
        )
        self.assertTrue(external["external"])
        self.assertNotIn("example-private", json.dumps(plan))
        self.assertNotIn("arr.example.invalid", json.dumps(external))
        self.assertEqual(
            [
                row["action"]
                for row in plan["actions"]
                if row["source"] == external["id"]
            ],
            ["external_ready", "indexers", "workflow_test"],
        )
        self.assertEqual(DEFAULTS, before)

    def test_preview_is_read_only_and_optional_probes_never_auto_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            inspected, applied = [], []

            def inspect(action, payload, operation):
                inspected.append((action["id"], operation))
                return {
                    "execution": "planned",
                    "verification": "not_checked",
                    "evidence": {
                        "preview_id": "opaque-example",
                        "changes": [
                            {"field": "enabled", "before": False, "after": True}
                        ],
                    },
                }

            def apply(action, payload, operation):
                applied.append((action["id"], operation))
                return {"execution": "applied", "verification": "passed"}

            coordinator = SetupCoordinator(
                directory,
                configuration=lambda: copy.deepcopy(DEFAULTS),
                defaults=lambda: DEFAULTS,
                start_services=lambda payload: {},
                inspect_action=inspect,
                apply_action=apply,
                autostart=False,
            )
            self.addCleanup(coordinator.shutdown)
            plan = coordinator.plan({"core_services": [{"name": "plex"}]})
            run = coordinator.start(plan["plan_id"], "example-key")
            coordinator.run_pending(run["run_id"])
            self.assertFalse(
                any(action == "plex::consumer_paths" for action, _ in applied)
            )
            applied.clear()
            coordinator.operate(
                run["run_id"], "preview", action_id="plex::consumer_paths"
            )
            coordinator.run_pending(run["run_id"])
            self.assertEqual(applied, [])
            self.assertIn(("plex::consumer_paths", "preview"), inspected)
            action = next(
                a
                for a in coordinator.get_run(run["run_id"])["actions"]
                if a["id"] == "plex::consumer_paths"
            )
            self.assertEqual(action["evidence"]["preview_status"], "ready")
            coordinator.operate(
                run["run_id"], "apply", action_id="plex::consumer_paths"
            )
            coordinator.run_pending(run["run_id"])
            self.assertEqual(applied, [("plex::consumer_paths", "apply")])


if __name__ == "__main__":
    unittest.main()
