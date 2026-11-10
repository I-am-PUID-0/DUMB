"""Selected optional features remain part of the accepted coverage scope."""

import copy
import tempfile
import unittest

from utils.arrangement_plan import coverage
from utils.arrangement_requirements import refresh_requirements
from utils.arrangement_state import SetupCoordinator
from tests.test_arrangement_plan_state import DEFAULTS


def actions():
    return [
        {
            "id": "maintainerr::seerr_connection",
            "source": "maintainerr",
            "service_key": "maintainerr",
            "action": "seerr_connection",
            "required": False,
            "automation": "after_input",
        },
        {
            "id": "maintainerr::tautulli_connection",
            "source": "maintainerr",
            "service_key": "maintainerr",
            "action": "tautulli_connection",
            "required": False,
            "automation": "after_input",
        },
        {
            "id": "decypharr::nntp",
            "source": "decypharr",
            "service_key": "decypharr",
            "action": "nntp",
            "required": False,
            "automation": "after_input",
        },
        {
            "id": "plex::account",
            "source": "plex",
            "service_key": "plex",
            "action": "account",
            "required": True,
            "automation": "manual",
        },
    ]


class ArrangementRequirementTests(unittest.TestCase):
    def test_workflow_target_switch_drops_previous_provider_and_media_dependencies(
        self,
    ):
        probe = {
            "id": "radarr.Test::workflow_test",
            "source": "radarr.Test",
            "service_key": "radarr",
            "action": "workflow_test",
            "required": False,
            "prerequisites": ["radarr.Test::runtime"],
        }
        identifiers = [
            "radarr.Test::runtime",
            "radarr.Test::provider.altmount",
            "radarr.Test::provider.infinidysk",
            "altmount::runtime",
            "altmount::account",
            "altmount::webdav",
            "infinidysk::runtime",
            "jellyfin::runtime",
            "jellyfin::account",
            "jellyfin::libraries",
            "plex::runtime",
            "plex::account",
            "plex::libraries",
        ]
        record = {
            "actions": [probe] + [{"id": identifier} for identifier in identifiers]
        }
        payload = {
            "arrangement": {
                "values": {
                    "radarr.Test::workflow_probe": {
                        "provider": "altmount",
                        "media_server": "jellyfin",
                    }
                }
            }
        }
        refresh_requirements(record, payload)
        self.assertIn("altmount::webdav", probe["prerequisites"])
        payload["arrangement"]["values"]["radarr.Test::workflow_probe"] = {
            "provider": "infinidysk",
            "media_server": "plex",
        }
        refresh_requirements(record, payload)
        self.assertEqual(
            set(probe["prerequisites"]),
            {
                "radarr.Test::runtime",
                "radarr.Test::provider.infinidysk",
                "infinidysk::runtime",
                "plex::runtime",
                "plex::account",
                "plex::libraries",
            },
        )

    def test_standalone_riven_media_switch_replaces_only_target_dependencies(self):
        connection = {
            "id": "riven_backend::media_connection",
            "source": "riven_backend",
            "service_key": "riven_backend",
            "action": "media_connection",
            "required": False,
            "prerequisites": ["riven_backend::runtime"],
        }
        record = {
            "actions": [connection]
            + [
                {"id": identifier}
                for identifier in (
                    "riven_backend::backend_connection",
                    "plex::runtime",
                    "plex::account",
                    "emby::runtime",
                    "emby::account",
                )
            ]
        }
        payload = {"arrangement": {"values": {"riven_backend::media_server": "plex"}}}
        refresh_requirements(record, payload)
        self.assertTrue(connection["required"])
        payload["arrangement"]["values"]["riven_backend::media_server"] = "emby"
        refresh_requirements(record, payload)
        self.assertEqual(
            set(connection["prerequisites"]),
            {
                "riven_backend::runtime",
                "riven_backend::backend_connection",
                "emby::runtime",
                "emby::account",
            },
        )

    def test_unselected_optional_work_is_excluded_but_selected_connections_are_counted(
        self,
    ):
        record = {"actions": actions()}
        identifiers = [item["id"] for item in record["actions"]]
        refresh_requirements(record, {"arrangement": {"values": {}}})
        self.assertEqual(coverage(record["actions"])["denominator"], 1)
        refresh_requirements(
            record,
            {
                "arrangement": {
                    "values": {
                        "maintainerr::seerr_instance": "seerr.Requests",
                        "maintainerr::connect_tautulli": True,
                        "decypharr::nntp_source": "infinidysk",
                    }
                }
            },
        )
        self.assertEqual(coverage(record["actions"])["denominator"], 4)
        self.assertEqual([item["id"] for item in record["actions"]], identifiers)

    def test_accounts_or_workflow_intent_require_decypharr_nntp(self):
        for arrangement in (
            {"values": {"decypharr::nntp": [{"host": "news.example.com"}]}},
            {"values": {}, "intent": {"stack": "usenet"}},
            {"values": {}, "intent": {"stack": "both"}},
        ):
            with self.subTest(arrangement=arrangement):
                record = {"actions": actions()}
                refresh_requirements(record, {"arrangement": arrangement})
                self.assertTrue(record["actions"][2]["required"])

    def test_deferring_or_removing_optional_choices_never_removes_base_required_work(
        self,
    ):
        record = {"actions": actions()}
        record["actions"][2].update(required=True, execution="declined")
        refresh_requirements(
            record, {"arrangement": {"values": {"decypharr::nntp": []}}}
        )
        self.assertTrue(record["actions"][2]["required"])
        self.assertEqual(record["actions"][2]["execution"], "declined")
        self.assertEqual(coverage(record["actions"])["denominator"], 2)

    def test_retained_connection_selection_increments_revision_and_coverage_not_action_ids(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:

            def fields(resource, config, payload):
                if resource["service_key"] != "maintainerr":
                    return []
                return [
                    {
                        "key": resource["id"] + "::connect_tautulli",
                        "resource_id": resource["id"],
                        "type": "boolean",
                        "label": "Connect Tautulli",
                    }
                ]

            manager = SetupCoordinator(
                directory,
                configuration=lambda: copy.deepcopy(DEFAULTS),
                defaults=lambda: DEFAULTS,
                start_services=lambda payload: None,
                inspect_action=lambda action, payload, operation: (
                    {"execution": "already_present", "verification": "passed"}
                    if action["action"] == "runtime"
                    else {"execution": "waiting_input", "verification": "unavailable"}
                ),
                discover_fields=fields,
                autostart=False,
            )
            try:
                payload = {"core_services": [], "optional_services": ["maintainerr"]}
                plan = manager.plan(payload)
                run = manager.start(plan["plan_id"], "optional-feature-revision")
                manager.run_pending(run["run_id"])
                before = manager.get_run(run["run_id"])
                target = next(
                    item
                    for item in before["actions"]
                    if item["action"] == "tautulli_connection"
                )
                manager.operate(
                    run["run_id"],
                    "apply",
                    action_id=target["id"],
                    values={"maintainerr::connect_tautulli": True},
                )
                changed = manager.get_run(run["run_id"])
                self.assertEqual(changed["revision"], before["revision"] + 1)
                self.assertEqual(
                    changed["coverage"]["denominator"],
                    before["coverage"]["denominator"] + 1,
                )
                self.assertEqual(
                    [item["id"] for item in changed["actions"]],
                    [item["id"] for item in before["actions"]],
                )
                manager.run_pending(run["run_id"])
                deferred = manager.operate(
                    run["run_id"], "defer", action_id=target["id"]
                )
                saved = next(
                    item for item in deferred["actions"] if item["id"] == target["id"]
                )
                self.assertTrue(saved["required"])
                self.assertEqual(saved["execution"], "waiting_input")
                self.assertEqual(
                    deferred["coverage"]["denominator"],
                    changed["coverage"]["denominator"],
                )
            finally:
                manager.shutdown()
