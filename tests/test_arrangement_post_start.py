"""Post-start guidance reuses retained actions without replaying installation."""

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from utils.arrangement_plan import (
    build_plan,
    project_configuration,
    refresh_post_start_tasks,
)
from utils.arrangement_state import SetupCoordinator

DEFAULTS = json.loads(
    (Path(__file__).resolve().parents[1] / "utils/dumb_config.json").read_text()
)


def request():
    return {
        "core_services": [
            {"name": "radarr", "instance_name": "Movies"},
            {"name": "sonarr", "instance_name": "Shows"},
            {"name": "plex"},
        ]
    }


class PostStartTaskTests(unittest.TestCase):
    def test_exact_selected_managed_arrs_receive_optional_manual_plex_tasks(self):
        plan = build_plan(request(), DEFAULTS, DEFAULTS, plan_id="a" * 32)
        tasks = [a for a in plan["actions"] if a["action"] == "plex_notifications"]
        self.assertEqual(
            {a["source"] for a in tasks}, {"radarr.Movies", "sonarr.Shows"}
        )
        for task in tasks:
            self.assertFalse(task["required"])
            self.assertEqual(task["automation"], "manual")
            self.assertEqual(task["target"], "plex")
            self.assertEqual(task["input_fields"], [])
            self.assertEqual(
                task["prerequisites"], [task["source"] + "::runtime", "plex::runtime"]
            )
            self.assertIn("normal Plex library monitoring", task["reason"])
            self.assertIn("does not create or independently verify", task["reason"])
            self.assertIsInstance(task["instructions"], list)
            self.assertEqual(len(task["instructions"]), 4)
            self.assertTrue(all(isinstance(step, str) for step in task["instructions"]))
            self.assertIn("Settings > Connect", task["instructions"][0])
            self.assertIn("Test", task["instructions"][2])

    def test_unselected_or_external_resources_never_acquire_a_native_plex_task(self):
        config, _ = project_configuration(request(), DEFAULTS, DEFAULTS)
        candidates = [
            [],
            [{"id": "radarr.Movies", "service_key": "radarr"}],
            [{"id": "plex", "service_key": "plex"}],
            [
                {"id": "plex", "service_key": "plex"},
                {
                    "id": "external_arr.movies",
                    "service_key": "radarr",
                    "external": True,
                },
            ],
            [
                {"id": "plex", "service_key": "plex", "external": True},
                {"id": "radarr.Movies", "service_key": "radarr"},
            ],
        ]
        for services in candidates:
            record = {"services": services, "actions": []}
            refresh_post_start_tasks(record, config)
            self.assertEqual(record["actions"], [])
        config["plex"]["enabled"] = False
        plan = build_plan(request(), DEFAULTS, DEFAULTS, plan_id="a" * 32)
        plan["actions"] = []
        refresh_post_start_tasks(plan, config)
        self.assertEqual(plan["actions"], [])

    def test_enrichment_preserves_an_existing_optional_decision(self):
        config, _ = project_configuration(request(), DEFAULTS, DEFAULTS)
        plan = build_plan(request(), DEFAULTS, DEFAULTS, plan_id="a" * 32)
        task = next(a for a in plan["actions"] if a["action"] == "plex_notifications")
        for decision in (
            {"execution": "declined", "verification": "not_checked"},
            {
                "execution": "applied",
                "verification": "unavailable",
                "evidence": {"level": "user_attested"},
            },
        ):
            task.update(decision)
            before = copy.deepcopy(plan)
            refresh_post_start_tasks(plan, config)
            self.assertEqual(plan, before)

    def test_manual_plex_task_never_contacts_or_changes_native_apps(self):
        from utils.arrangement_runtime import ArrangementRuntime

        config, _ = project_configuration(request(), DEFAULTS, DEFAULTS)
        plan = build_plan(request(), DEFAULTS, DEFAULTS, plan_id="a" * 32)
        task = next(a for a in plan["actions"] if a["action"] == "plex_notifications")
        manager = SimpleNamespace(config=config, save_config=Mock())
        runtime = ArrangementRuntime(manager, Mock(), Mock(), Mock())
        before = copy.deepcopy(config)
        with patch(
            "utils.arrangement_http.ApiClient.request",
            side_effect=AssertionError("manual task contacted an app"),
        ):
            for operation in (runtime.inspect, runtime.apply):
                result = operation(task, request())
                self.assertEqual(result["execution"], "waiting_input")
                self.assertEqual(result["verification"], "unavailable")
                self.assertFalse(result["retryable"])
        self.assertEqual(config, before)
        manager.save_config.assert_not_called()


class RetainedRunDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config, _ = project_configuration(request(), DEFAULTS, DEFAULTS)
        self.started, self.applied, self.inspected, self.discovered = [], [], [], []
        self.ready = False

        def discover(resource, config, payload):
            self.discovered.append(resource["id"])
            if resource["id"] != "radarr.Movies":
                return []
            return [
                {
                    "key": "radarr.Movies::quality_profile",
                    "resource_id": "radarr.Movies",
                    "type": "select",
                    "options": (
                        [{"value": 7, "label": "Native profile"}] if self.ready else []
                    ),
                }
            ]

        def inspect(action, payload, operation):
            self.inspected.append(action["id"])
            return {
                "execution": (
                    "already_present"
                    if action["action"] == "runtime"
                    else "waiting_input"
                ),
                "verification": (
                    "passed" if action["action"] == "runtime" else "not_checked"
                ),
            }

        def apply(action, payload, operation):
            self.applied.append(action["id"])
            return {"execution": "waiting_input", "verification": "not_checked"}

        self.coordinator = SetupCoordinator(
            self.temp.name,
            configuration=lambda: self.config,
            defaults=lambda: DEFAULTS,
            start_services=lambda payload: self.started.append(payload),
            inspect_action=inspect,
            apply_action=apply,
            discover_fields=discover,
            autostart=False,
        )
        self.addCleanup(self.coordinator.shutdown)

    def run_setup(self):
        plan = self.coordinator.plan(request())
        run = self.coordinator.start(plan["plan_id"], "post-start-example")
        self.coordinator.run_pending(run["run_id"])
        return self.coordinator.get_run(run["run_id"])

    def test_explicit_action_recheck_refreshes_choices_without_startup_or_apply(self):
        run = self.run_setup()
        self.ready = True
        self.started.clear()
        self.applied.clear()
        self.inspected.clear()
        self.coordinator.operate(
            run["run_id"], "recheck", action_id="radarr.Movies::runtime"
        )
        self.coordinator.run_pending(run["run_id"])
        refreshed = self.coordinator.get_run(run["run_id"])
        field = next(
            f
            for f in refreshed["arrangement_schema"]["fields"]
            if f["key"] == "radarr.Movies::quality_profile"
        )
        self.assertEqual(field["options"], [{"value": 7, "label": "Native profile"}])
        self.assertEqual(self.inspected, ["radarr.Movies::runtime"])
        self.assertEqual(self.started, [])
        self.assertEqual(self.applied, [])
        before = (len(self.discovered), len(self.inspected))
        self.coordinator.get_run(run["run_id"])
        self.coordinator.latest()
        self.assertEqual((len(self.discovered), len(self.inspected)), before)

    def test_existing_run_gains_manual_task_and_retains_defer_or_attestation(self):
        run = self.run_setup()
        saved = self.coordinator.store.read("run", run["run_id"])
        saved["actions"] = [
            a for a in saved["actions"] if a["action"] != "plex_notifications"
        ]
        self.coordinator.store.write("run", run["run_id"], saved)
        self.started.clear()
        self.applied.clear()
        self.coordinator.operate(
            run["run_id"], "recheck", action_id="radarr.Movies::runtime"
        )
        self.coordinator.run_pending(run["run_id"])
        enriched = self.coordinator.get_run(run["run_id"])
        tasks = [a for a in enriched["actions"] if a["action"] == "plex_notifications"]
        self.assertEqual(len(tasks), 2)
        self.assertEqual(self.started, [])
        self.assertEqual(self.applied, [])
        task_id = "radarr.Movies::plex_notifications"
        deferred = self.coordinator.operate(run["run_id"], "defer", action_id=task_id)
        self.assertEqual(
            next(a for a in deferred["actions"] if a["id"] == task_id)["execution"],
            "declined",
        )
        confirmed = self.coordinator.operate(
            run["run_id"], "confirm", action_id=task_id
        )
        task = next(a for a in confirmed["actions"] if a["id"] == task_id)
        self.assertEqual(task["execution"], "applied")
        self.assertEqual(task["verification"], "unavailable")
        self.assertEqual(task["evidence"], {"level": "user_attested"})

    def test_user_deferral_survives_get_global_recheck_and_schema_refresh(self):
        run = self.run_setup()
        required = next(
            a
            for a in run["actions"]
            if a["required"] and a["automation"] != "automatic"
        )
        task_ids = [required["id"], "radarr.Movies::plex_notifications"]
        for identifier in task_ids:
            deferred = self.coordinator.operate(
                run["run_id"], "defer", action_id=identifier
            )
            task = next(a for a in deferred["actions"] if a["id"] == identifier)
            self.assertIs(task["deferred_by_user"], True)
            self.assertEqual(
                task["execution"], "waiting_input" if task["required"] else "declined"
            )
        self.started.clear()
        self.applied.clear()
        self.coordinator.operate(run["run_id"], "recheck")
        self.coordinator.run_pending(run["run_id"])
        self.coordinator._refresh_schema(run["run_id"], request())
        refreshed = self.coordinator.get_run(run["run_id"])
        for identifier in task_ids:
            task = next(a for a in refreshed["actions"] if a["id"] == identifier)
            self.assertIs(task["deferred_by_user"], True)
            self.assertIsNone(task["next_retry_at"])
            self.assertFalse(task["retryable"])
        self.assertEqual(self.started, [])
        self.assertEqual(self.applied, [])
        self.coordinator.operate(run["run_id"], "retry")
        self.coordinator.run_pending(run["run_id"])
        self.assertTrue(all(identifier not in self.applied for identifier in task_ids))
        self.assertEqual(self.started, [])

    def test_only_an_accepted_targeted_operation_clears_user_deferral(self):
        run = self.run_setup()
        task_id = "radarr.Movies::plex_notifications"
        for operation in ("preview", "apply", "recheck", "retry", "confirm"):
            with self.subTest(operation=operation):
                self.coordinator.operate(run["run_id"], "defer", action_id=task_id)
                accepted = self.coordinator.operate(
                    run["run_id"], operation, action_id=task_id
                )
                task = next(a for a in accepted["actions"] if a["id"] == task_id)
                self.assertIs(task["deferred_by_user"], False)
                if operation != "confirm":
                    self.coordinator.run_pending(run["run_id"])

    def test_rejected_inputs_or_busy_run_do_not_clear_user_deferral(self):
        from utils.arrangement_state import SetupConflict

        run = self.run_setup()
        task_id = "radarr.Movies::plex_notifications"
        self.coordinator.operate(run["run_id"], "defer", action_id=task_id)
        for values in (
            {"radarr.Movies::unadvertised": "value"},
            {"radarr.Movies::api_key": "[configured]"},
            {"radarr.Movies::battery_mode": "a-different-topology"},
        ):
            with self.subTest(values=list(values)):
                with self.assertRaises(ValueError):
                    self.coordinator.operate(
                        run["run_id"], "apply", action_id=task_id, values=values
                    )
                task = next(
                    a
                    for a in self.coordinator.get_run(run["run_id"])["actions"]
                    if a["id"] == task_id
                )
                self.assertIs(task["deferred_by_user"], True)
        self.coordinator.operate(
            run["run_id"], "recheck", action_id="radarr.Movies::runtime"
        )
        with self.assertRaises(SetupConflict):
            self.coordinator.operate(run["run_id"], "recheck", action_id=task_id)
        task = next(
            a
            for a in self.coordinator.get_run(run["run_id"])["actions"]
            if a["id"] == task_id
        )
        self.assertIs(task["deferred_by_user"], True)
        self.coordinator.run_pending(run["run_id"])

    def test_global_retry_does_not_restart_a_deferred_failed_runtime(self):
        run = self.run_setup()
        runtime_id = "radarr.Movies::runtime"
        self.coordinator._save_action(
            run["run_id"], runtime_id, {"execution": "failed", "verification": "failed"}
        )
        self.coordinator.operate(run["run_id"], "defer", action_id=runtime_id)
        self.started.clear()
        self.coordinator.operate(run["run_id"], "retry")
        self.coordinator.run_pending(run["run_id"])
        self.assertEqual(self.started, [])

    def test_global_recheck_preserves_manual_attestation_until_targeted_recheck(self):
        run = self.run_setup()
        task_id = "radarr.Movies::plex_notifications"
        self.coordinator.operate(run["run_id"], "confirm", action_id=task_id)
        self.started.clear()
        self.applied.clear()
        self.inspected.clear()
        discoveries = len(self.discovered)
        self.coordinator.operate(run["run_id"], "recheck")
        self.coordinator.run_pending(run["run_id"])
        refreshed = self.coordinator.get_run(run["run_id"])
        task = next(a for a in refreshed["actions"] if a["id"] == task_id)
        self.assertEqual(task["execution"], "applied")
        self.assertEqual(task["verification"], "unavailable")
        self.assertEqual(task["evidence"], {"level": "user_attested"})
        self.assertNotIn(task_id, self.inspected)
        self.assertGreater(len(self.discovered), discoveries)
        self.assertEqual(self.started, [])
        self.assertEqual(self.applied, [])
        self.coordinator.operate(run["run_id"], "recheck", action_id=task_id)
        self.coordinator.run_pending(run["run_id"])
        checked = self.coordinator.get_run(run["run_id"])
        task = next(a for a in checked["actions"] if a["id"] == task_id)
        self.assertEqual(task["execution"], "waiting_input")
        self.assertEqual(task["verification"], "not_checked")
        self.assertIsNone(task["evidence"])
        self.assertIn(task_id, self.inspected)
