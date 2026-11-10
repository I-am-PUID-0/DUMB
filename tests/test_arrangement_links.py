"""Per-target connection units preserve complete desired scope on retries."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from tests import test_arrangement_connections as connection_fixtures
from tests import test_arrangement_helpers as helper_fixtures
from tests import test_setup_arr_reconciliation as reconciliation_fixtures
from tests.test_arrangement_plan_state import DEFAULTS
from utils import arrangement_connections as connections
from utils import arrangement_helpers as helpers
from utils import arrangement_links as links
from utils import prowlarr_settings, profilarr_settings
from utils.arrangement_link_actions import refresh_link_actions
from utils.arrangement_plan import coverage
from utils.arrangement_state import SetupCoordinator


def record(source, targets):
    family = source.partition(".")[0]
    resources = [
        {
            "id": source,
            "service_key": family,
            "instance_key": source.partition(".")[2],
            "name": source,
        }
    ] + [
        {"id": target, "service_key": target.partition(".")[0], "name": target}
        for target in targets
    ]
    runtime = [
        {
            "id": resource["id"] + "::runtime",
            "source": resource["id"],
            "service_key": resource["service_key"],
            "action": "runtime",
            "automation": "automatic",
            "required": True,
            "execution": "already_present",
            "verification": "passed",
        }
        for resource in resources
    ]
    group = {
        "id": source + "::arr_connections",
        "source": source,
        "service_key": family,
        "instance_key": source.partition(".")[2],
        "action": "arr_connections",
        "automation": "after_input",
        "required": True,
        "execution": "applied",
        "verification": "unavailable",
        "input_fields": [source + "::arr_instances"],
    }
    return {
        "services": resources,
        "actions": runtime + [group],
        "arrangement_schema": {
            "fields": [
                {
                    "key": source + "::arr_instances",
                    "type": "multiselect",
                    "resource_id": source,
                    "options": [{"value": target} for target in targets],
                }
            ]
        },
    }


def leaf(source, target, **extra):
    return {
        "id": source + "::arr_connection::" + target,
        "source": source,
        "service_key": source.partition(".")[0],
        "instance_key": source.partition(".")[2],
        "target": target,
        "action": "arr_connection",
        **extra,
    }


class ArrLinkScopeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = connection_fixtures.ArrangementConnectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config, self.api = self.fixture.config, self.fixture.api
        self.source = "seerr.Requests"
        self.payload = self.fixture.payload(
            self.source, ("radarr.Movies", "sonarr.Shows")
        )

    def test_each_exact_source_target_is_a_required_distinct_unit(self):
        plan = record(self.source, ["radarr.Movies", "sonarr.Shows"])
        refresh_link_actions(plan, self.payload, self.config)
        leaves = [
            action for action in plan["actions"] if action["action"] == "arr_connection"
        ]
        self.assertEqual(len(leaves), 2)
        self.assertEqual(coverage(leaves)["denominator"], 2)
        self.assertTrue(all(action["required"] for action in leaves))
        self.assertNotEqual(leaves[0]["id"], leaves[1]["id"])
        self.assertEqual(
            leaves[0]["prerequisites"],
            [
                self.source + "::arr_connections",
                self.source + "::runtime",
                leaves[0]["target"] + "::runtime",
            ],
        )
        group = next(
            action
            for action in plan["actions"]
            if action["action"] == "arr_connections"
        )
        self.assertEqual(group["connection_scope"], "assignment")
        self.assertTrue(group["coverage_counted"])
        # Two request-server instances do not share their target action identity.
        second = record("seerr.FourK", ["radarr.Movies"])
        second_payload = self.fixture.payload("seerr.FourK")
        refresh_link_actions(second, second_payload, self.config)
        self.assertNotEqual(leaves[0]["id"], second["actions"][-1]["id"])

    def test_targeted_upsert_and_recheck_never_contact_other_selected_target(self):
        result = links.apply_link(
            leaf(self.source, "radarr.Movies"),
            self.config,
            self.payload,
            client_factory=self.api.factory,
            ownership_directory=str(self.fixture.owner),
        )
        self.assertEqual(result["verification"], "passed", result)
        self.assertEqual(len(self.api.rows["seerr"]["radarr"]), 1)
        self.assertEqual(self.api.rows["seerr"]["sonarr"], [])
        self.assertFalse(
            any(
                ":8989" in call[0] or "/settings/sonarr" in call[2]
                for call in self.api.calls
            )
        )
        self.api.calls.clear()
        result = links.inspect_link(
            leaf(self.source, "radarr.Movies"),
            self.config,
            self.payload,
            client_factory=self.api.factory,
            ownership_directory=str(self.fixture.owner),
        )
        self.assertEqual(result["verification"], "passed", result)
        self.assertEqual(self.api.writes(), [])
        self.assertFalse(
            any(
                ":8989" in call[0] or "/settings/sonarr" in call[2]
                for call in self.api.calls
            )
        )

    def test_partial_retry_and_revision_keep_other_target_state_and_existing_entries(
        self,
    ):
        config = copy.deepcopy(DEFAULTS)
        config.update(copy.deepcopy(self.config))
        payload = copy.deepcopy(self.payload)
        payload.update(
            core_services=[
                {"name": "seerr", "instance_name": "Requests"},
                {"name": "radarr", "instance_name": "Movies"},
                {"name": "sonarr", "instance_name": "Shows"},
            ],
            optional_services=[],
        )

        def callback(action, request, operation):
            if action["action"] == "runtime":
                return {"execution": "already_present", "verification": "passed"}
            if action["action"] == "arr_connections":
                return {
                    "execution": "applied",
                    "verification": "unavailable",
                    "automation": "after_input",
                }
            if action["action"] == "arr_connection":
                function = (
                    links.inspect_link if operation == "recheck" else links.apply_link
                )
                return function(
                    action,
                    config,
                    request,
                    operation,
                    client_factory=self.api.factory,
                    ownership_directory=str(self.fixture.owner),
                )
            return {"execution": "waiting_input", "verification": "unavailable"}

        with tempfile.TemporaryDirectory() as state:
            manager = SetupCoordinator(
                state,
                configuration=lambda: config,
                defaults=lambda: DEFAULTS,
                start_services=lambda payload: None,
                inspect_action=callback,
                apply_action=callback,
                discover_fields=connections.schema_fields,
                autostart=False,
            )
            try:
                plan = manager.plan(payload)
                refresh_link_actions(plan, payload, config)
                manager.store.write("plan", plan["plan_id"], plan)
                self.api.api_failures["/api/v1/settings/sonarr"] = (503, {})
                run = manager.start(plan["plan_id"], "per-target-partial-retry")
                manager.run_pending(run["run_id"])
                first = manager.get_run(run["run_id"])
                radarr_id = leaf(self.source, "radarr.Movies")["id"]
                sonarr_id = leaf(self.source, "sonarr.Shows")["id"]
                by_id = {action["id"]: action for action in first["actions"]}
                self.assertEqual(
                    by_id[radarr_id]["verification"],
                    "passed",
                    [
                        (item["id"], item.get("execution"), item.get("prerequisites"))
                        for item in first["actions"]
                    ],
                )
                self.assertEqual(by_id[sonarr_id]["execution"], "failed")
                saved_movies = copy.deepcopy(self.api.rows["seerr"]["radarr"])
                self.api.api_failures.clear()
                self.api.calls.clear()
                manager.operate(run["run_id"], "retry")
                manager.run_pending(run["run_id"])
                self.assertEqual(self.api.rows["seerr"]["radarr"], saved_movies)
                self.assertFalse(
                    any(
                        ":7878" in call[0] or "/settings/radarr" in call[2]
                        for call in self.api.calls
                    )
                )
                self.assertEqual(
                    next(
                        action
                        for action in manager.get_run(run["run_id"])["actions"]
                        if action["id"] == sonarr_id
                    )["verification"],
                    "passed",
                )
                # Editing one link's policy leaves the shared assignment satisfied.
                manager.operate(
                    run["run_id"],
                    "apply",
                    action_id=sonarr_id,
                    values={self.source + "::sonarr.Shows::quality_profile": 4},
                )
                pending = manager.get_run(run["run_id"])
                self.assertEqual(
                    next(
                        action
                        for action in pending["actions"]
                        if action["id"] == self.source + "::arr_connections"
                    )["execution"],
                    "applied",
                )
                self.assertEqual(
                    next(
                        action
                        for action in pending["actions"]
                        if action["id"] == radarr_id
                    )["verification"],
                    "passed",
                )
                manager.run_pending(run["run_id"])
                self.assertEqual(self.api.rows["seerr"]["radarr"], saved_movies)
                # A changed assignment creates a new revision and cannot reuse
                # verified evidence for the changed membership.
                prior = manager.get_run(run["run_id"])
                manager.operate(
                    run["run_id"],
                    "apply",
                    action_id=self.source + "::arr_connections",
                    values={self.source + "::arr_instances": ["sonarr.Shows"]},
                )
                changed = manager.get_run(run["run_id"])
                self.assertEqual(changed["revision"], prior["revision"] + 1)
                removed = next(
                    action for action in changed["actions"] if action["id"] == radarr_id
                )
                self.assertFalse(removed["required"])
                self.assertEqual(removed["execution"], "declined")
                self.assertIsNone(removed["evidence"])
                manager.run_pending(run["run_id"])
                self.assertEqual(self.api.rows["seerr"]["radarr"], saved_movies)
            finally:
                manager.shutdown()

    def test_real_runtime_schema_and_plan_retain_all_four_prowlarr_families(self):
        import types
        from unittest.mock import Mock
        from utils.arrangement_plan import build_plan, project_configuration
        from utils.arrangement_runtime import ArrangementRuntime

        config = copy.deepcopy(DEFAULTS)
        source = "prowlarr.Index"
        targets = ("radarr.Movies", "sonarr.Shows", "lidarr.Music", "whisparr.Video")
        payload = {
            "core_services": [{"name": "prowlarr", "instance_name": "Index"}]
            + [
                {
                    "name": target.partition(".")[0],
                    "instance_name": target.partition(".")[2],
                    "service_options": {
                        target.partition(".")[0]: {
                            "use_neutarr": False,
                            "use_profilarr": False,
                        }
                    },
                }
                for target in targets
            ],
            "optional_services": [],
            "arrangement": {"values": {source + "::arr_instances": list(targets)}},
        }
        runtime = ArrangementRuntime(
            types.SimpleNamespace(config=config),
            Mock(),
            types.SimpleNamespace(get_status=lambda name: "stopped"),
            Mock(),
        )
        proposed, resources = project_configuration(payload, config, DEFAULTS)
        resource = next(item for item in resources if item["id"] == source)
        raw_fields = runtime.schema_fields(resource, proposed, payload)
        assignments = [
            field for field in raw_fields if field["key"] == source + "::arr_instances"
        ]
        self.assertEqual(len(assignments), 1)
        self.assertEqual(
            {item["value"] for item in assignments[0]["options"]}, set(targets)
        )
        plan = build_plan(
            payload,
            config,
            DEFAULTS,
            plan_id="a" * 32,
            discover_fields=runtime.schema_fields,
        )
        assignment = next(
            field
            for field in plan["arrangement_schema"]["fields"]
            if field["key"] == source + "::arr_instances"
        )
        self.assertEqual(
            {item["value"] for item in assignment["options"]}, set(targets)
        )
        leaves = [
            action
            for action in plan["actions"]
            if action["action"] == "arr_connection" and action["source"] == source
        ]
        self.assertEqual({action["target"] for action in leaves}, set(targets))
        self.assertTrue(all(action["required"] for action in leaves))

    def test_absent_assignment_is_not_an_explicit_empty_selection(self):
        plan = record(self.source, ["radarr.Movies", "sonarr.Shows"])
        refresh_link_actions(plan, {"arrangement": {"values": {}}}, self.config)
        leaves = [
            item for item in plan["actions"] if item["action"] == "arr_connection"
        ]
        self.assertTrue(
            all(
                item["execution"] == "waiting_input" and not item["required"]
                for item in leaves
            )
        )
        refresh_link_actions(
            plan,
            {"arrangement": {"values": {self.source + "::arr_instances": []}}},
            self.config,
        )
        self.assertTrue(
            all(
                item["execution"] == "declined" and not item["required"]
                for item in leaves
            )
        )

    def test_failed_helper_retirement_never_disappears_from_required_scope(self):
        plan = record("neutarr.Automation", ["radarr.Movies", "sonarr.Shows"])
        payload = {
            "arrangement": {
                "values": {
                    "neutarr.Automation::arr_instances": [
                        "radarr.Movies",
                        "sonarr.Shows",
                    ]
                }
            }
        }
        refresh_link_actions(plan, payload, self.config)
        payload["arrangement"]["values"]["neutarr.Automation::arr_instances"] = [
            "sonarr.Shows"
        ]
        refresh_link_actions(plan, payload, self.config)
        removed = next(
            action
            for action in plan["actions"]
            if action.get("target") == "radarr.Movies"
        )
        self.assertTrue(removed["required"])
        self.assertTrue(removed["retirement_pending"])
        removed.update(
            execution="waiting_input",
            verification="unavailable",
            reason="Ownership needs review",
            retryable=False,
        )
        refresh_link_actions(plan, payload, self.config)
        self.assertTrue(removed["required"])
        self.assertEqual(removed["execution"], "waiting_input")
        self.assertEqual(removed["reason"], "Ownership needs review")


class ScopedHelperMutationTests(unittest.TestCase):
    def test_neutarr_native_ui_metadata_loss_does_not_make_active_retirement_pass(self):
        fixture = helper_fixtures.CompanionAdapterTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        second_xml = fixture.root / "second.xml"
        second_xml.write_text("<Config><ApiKey>fixture-second</ApiKey></Config>")
        fixture.config["radarr"]["instances"]["Other"] = {
            "enabled": True,
            "port": 7879,
            "config_file": str(second_xml),
        }
        values = fixture.payload["arrangement"]["values"]
        values["neutarr.Automation::arr_instances"] = ["radarr.Movies", "radarr.Other"]
        fixture.apply()
        path = fixture.root / "selected/config/radarr.json"
        saved = json.loads(path.read_text())
        for row in saved["instances"]:
            row["enabled"] = True
            for key in list(row):
                if key.startswith("_dumb"):
                    del row[key]
        path.write_text(json.dumps(saved))
        other = copy.deepcopy(saved["instances"][1])
        values["neutarr.Automation::arr_instances"] = ["radarr.Other"]
        action = leaf(
            "neutarr.Automation",
            "radarr.Movies",
            retirement_pending=True,
            retirement_required=True,
        )
        before = links.inspect_link(
            action, fixture.config, fixture.payload, manager=fixture.manager
        )
        self.assertEqual(before["execution"], "waiting_input", before)
        self.assertNotEqual(before["verification"], "passed")
        result = links.apply_link(
            action, fixture.config, fixture.payload, manager=fixture.manager
        )
        self.assertEqual(result["verification"], "passed", result)
        self.assertTrue(result["evidence"]["retired"])
        after = json.loads(path.read_text())["instances"]
        self.assertFalse(after[0]["enabled"])
        self.assertEqual(after[1], other)

    def test_prowlarr_scoped_retry_does_not_disable_or_update_other_desired_app(self):
        fixture = reconciliation_fixtures.ProwlarrReconciliationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.instance["arr_instances"] = ["radarr.movies", "radarr.uhd"]
        self.assertTrue(fixture.run_patch()[0][0])
        other = copy.deepcopy(fixture.api.apps[2])
        fixture.entries[0]["api_key"] = "fixture-rotated"
        fixture.entries[1]["core_services"] = ["decypharr"]
        # The other removed assignment has its own retirement action. Retrying
        # this edge must not retire that other owned entry implicitly.
        fixture.instance["arr_instances"] = ["radarr.movies"]
        fixture.api.calls.clear()
        result = prowlarr_settings.patch_prowlarr_apps(target_ids={"radarr.movies"})
        self.assertTrue(result[0], result)
        self.assertEqual(fixture.api.apps[2], other)
        writes = [
            call for call in fixture.api.calls if call[1] in {"PUT", "POST", "DELETE"}
        ]
        self.assertTrue(any(call[0].endswith("applications/1") for call in writes))
        self.assertFalse(any(call[0].endswith("applications/2") for call in writes))
        self.assertFalse(any(call[0].endswith("/tag") for call in writes))

    def test_profilarr_scoped_rows_retain_full_desired_set_during_cleanup(self):
        fixture = reconciliation_fixtures.ProfilarrV1ReconciliationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.insert("radarr:movies", ["dumb:auto"])
        fixture.insert("radarr:other", ["dumb:auto", "personal"])
        before = fixture.rows()["radarr:other"]
        entry = copy.deepcopy(fixture.entry)
        other_entry = {**entry, "name": "radarr:other", "api_key": "fixture-other"}
        from unittest import mock

        with (
            mock.patch.object(
                profilarr_settings,
                "_build_arr_entries",
                return_value=[entry, other_entry],
            ),
            mock.patch.object(
                profilarr_settings, "_target_arr_names", return_value={"radarr:movies"}
            ),
        ):
            result = profilarr_settings.sync_profilarr_arr_configs(
                fixture.instance, target_ids={"radarr.movies"}
            )
        self.assertTrue(result[0], result)
        self.assertEqual(fixture.rows()["radarr:other"], before)

    def test_prowlarr_unknown_ownership_does_not_verify_active_matching_retirement(
        self,
    ):
        fixture = helper_fixtures.CompanionAdapterTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source_xml = fixture.root / "prowlarr.xml"
        source_xml.write_text("<Config><ApiKey>fixture-source</ApiKey></Config>")
        fixture.config["prowlarr"] = {
            "instances": {
                "Index": {
                    "enabled": True,
                    "port": 9696,
                    "config_file": str(source_xml),
                    "config_dir": str(fixture.root / "prowlarr"),
                }
            }
        }
        from utils.arrangement_http import ApiResponse

        class Client:
            def request(self, method, path, **kwargs):
                return ApiResponse(
                    200,
                    json.dumps(
                        [
                            {
                                "id": 7,
                                "implementation": "Radarr",
                                "syncLevel": "fullSync",
                                "fields": [
                                    {
                                        "name": "baseUrl",
                                        "value": "http://127.0.0.1:7878/native",
                                    }
                                ],
                            }
                        ]
                    ).encode(),
                )

        payload = {"arrangement": {"values": {"prowlarr.Index::arr_instances": []}}}
        result = links.inspect_link(
            leaf("prowlarr.Index", "radarr.Movies", retirement_required=True),
            fixture.config,
            payload,
            client_factory=lambda *args, **kwargs: Client(),
        )
        self.assertEqual(result["execution"], "waiting_input", result)
        self.assertNotEqual(result["verification"], "passed")

    def test_profilarr_v2_scoped_update_and_retirement_keep_other_owned_rows(self):
        import sqlite3
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root = root / "config/data"
            config_root.mkdir(parents=True)
            (root / "runtime").mkdir()
            (root / "runtime/profilarr").write_text(
                "fixture runtime marker; never executed"
            )
            path = config_root / "profilarr.db"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE arr_instances (id INTEGER PRIMARY KEY, name TEXT UNIQUE, type TEXT, url TEXT, api_key TEXT, tags TEXT, enabled INTEGER, updated_at TEXT)"
                )
                for index, name in enumerate(("movies", "other"), 1):
                    connection.execute(
                        "INSERT INTO arr_instances VALUES (?,?,?,?,?,?,?,?)",
                        (
                            index,
                            "radarr:" + name,
                            "radarr",
                            "http://127.0.0.1:" + str(7877 + index),
                            "old-fixture",
                            json.dumps(["dumb:auto", "dumb:instance:radarr." + name]),
                            1,
                            "original",
                        ),
                    )

            def rows():
                with sqlite3.connect(path) as connection:
                    return {
                        row[0]: row
                        for row in connection.execute(
                            "SELECT name, type, url, api_key, tags, enabled, updated_at FROM arr_instances"
                        )
                    }

            config = {
                "puid": 0,
                "pgid": 0,
                "radarr": {"instances": {}},
                "profilarr": {
                    "instances": {
                        "Profiles": {
                            "enabled": True,
                            "port": 6868,
                            "config_dir": str(root),
                            "arr_instances": ["radarr.movies", "radarr.other"],
                        }
                    }
                },
            }
            for name, port in (("movies", 7878), ("other", 7879)):
                xml = root / (name + ".xml")
                xml.write_text("<Config><ApiKey>fixture-new</ApiKey></Config>")
                config["radarr"]["instances"][name] = {
                    "enabled": True,
                    "port": port,
                    "config_file": str(xml),
                }
            before = rows()["radarr:other"]
            with mock.patch.object(
                profilarr_settings, "detect_profilarr_layout", return_value="v2"
            ):
                result = profilarr_settings.patch_profilarr_config(
                    configuration=config, target_ids={"radarr.movies"}
                )
                self.assertTrue(result[0], result)
                self.assertEqual(rows()["radarr:other"], before)
                config["profilarr"]["instances"]["Profiles"]["arr_instances"] = []
                manager = helper_fixtures.Manager(config)
                result = links.apply_link(
                    leaf(
                        "profilarr.Profiles",
                        "radarr.movies",
                        retirement_required=True,
                        retirement_pending=True,
                    ),
                    config,
                    {
                        "arrangement": {
                            "values": {"profilarr.Profiles::arr_instances": []}
                        }
                    },
                    manager=manager,
                )
            self.assertEqual(result["verification"], "passed", result)
            self.assertTrue(result["evidence"]["retired"])
            self.assertEqual(rows(), {"radarr:other": before})
