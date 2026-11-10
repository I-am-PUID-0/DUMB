import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from utils.arrangement_plan import build_plan, coverage, public_value, resource_config
from utils.arrangement_state import SetupConflict, SetupCoordinator, SetupStore

DEFAULTS = json.loads(
    (Path(__file__).resolve().parents[1] / "utils/dumb_config.json").read_text()
)


def request():
    return {
        "core_services": [
            {"name": "infinidysk"},
            {
                "name": "radarr",
                "instance_name": "HD",
                "service_options": {"radarr": {"core_service": "infinidysk"}},
            },
            {"name": "plex"},
        ],
        "optional_services": [],
    }


class ArrangementPlanTests(unittest.TestCase):
    def test_plan_materializes_fresh_targets_without_mutating_configuration(self):
        initial = copy.deepcopy(DEFAULTS)
        before = copy.deepcopy(initial)
        plan = build_plan(request(), initial, DEFAULTS, plan_id="a" * 32)
        self.assertEqual(initial, before)
        ids = {resource["id"] for resource in plan["services"]}
        self.assertTrue({"infinidysk", "radarr.HD", "plex", "rclone.InfiniDysk"} <= ids)
        self.assertTrue(
            any(
                item["target"] == "infinidysk" and item["source"] == "radarr.HD"
                for item in plan["actions"]
            )
        )
        self.assertTrue(
            all(item["verification"] == "not_checked" for item in plan["actions"])
        )

    def test_multiple_mount_providers_have_distinct_required_resources(self):
        payload = request()
        payload["core_services"].append({"name": "riven_backend"})
        plan = build_plan(payload, DEFAULTS, DEFAULTS, plan_id="a" * 32)
        ids = [r["id"] for r in plan["services"]]
        self.assertIn("rclone.InfiniDysk", ids)
        self.assertIn("rclone.Riven Backend", ids)
        self.assertEqual(len(ids), len(set(ids)))

    def test_unsupported_or_ambiguous_instance_rejected(self):
        bad = {"core_services": [{"name": "unknown"}]}
        with self.assertRaises(ValueError):
            build_plan(bad, DEFAULTS, DEFAULTS, plan_id="a" * 32)
        bad = {"core_services": [{"name": "radarr", "instance_name": "../escape"}]}
        with self.assertRaises(ValueError):
            build_plan(bad, DEFAULTS, DEFAULTS, plan_id="a" * 32)

    def test_public_plan_contains_no_provided_secrets(self):
        payload = request()
        payload["arrangement"] = {
            "values": {
                "plex::api_key": "private-api-example",
                "infinidysk::nntp": [
                    {
                        "host": "news.example.com",
                        "username": "private-user-example",
                        "password": "private-pass-example",
                    }
                ],
            }
        }
        plan = build_plan(payload, DEFAULTS, DEFAULTS, plan_id="a" * 32)
        encoded = json.dumps(plan)
        for value in (
            "private-api-example",
            "private-user-example",
            "private-pass-example",
        ):
            self.assertNotIn(value, encoded)
        self.assertEqual(plan["values"]["plex::api_key"], "[configured]")
        self.assertIn(
            "plex::api_key",
            next(a for a in plan["actions"] if a["id"] == "plex::libraries")[
                "input_fields"
            ],
        )

    def test_optional_only_and_unknown_native_app_tasks_are_explicit(self):
        plan = build_plan(
            {"core_services": [], "optional_services": ["audiobookshelf"]},
            DEFAULTS,
            DEFAULTS,
            plan_id="a" * 32,
        )
        self.assertEqual([r["id"] for r in plan["services"]], ["audiobookshelf"])
        self.assertEqual(
            next(a for a in plan["actions"] if a["action"] == "account")["execution"],
            "waiting_input",
        )

    def test_coverage_keeps_required_manual_and_unknown_tasks(self):
        actions = [
            {
                "required": True,
                "automation": "automatic",
                "execution": "already_present",
                "verification": "passed",
            },
            {"required": True, "automation": "manual", "execution": "waiting_input"},
            {"required": True, "automation": "unknown", "execution": "skipped"},
            {"required": False, "automation": "manual", "execution": "declined"},
        ]
        result = coverage(actions)
        self.assertEqual(
            (
                result["denominator"],
                result["automatic"],
                result["manual"],
                result["unknown"],
                result["applied"],
                result["verified"],
            ),
            (3, 1, 1, 1, 1, 1),
        )

    def test_library_paths_and_unadvertised_inputs_rejected(self):
        for values in (
            {
                "plex::libraries": [
                    {"name": "Bad", "media_type": "movie", "paths": ["/config"]}
                ]
            },
            {"plex::not_advertised": "value"},
        ):
            payload = request()
            payload["arrangement"] = {"values": values}
            with self.assertRaises(ValueError):
                build_plan(payload, DEFAULTS, DEFAULTS, plan_id="a" * 32)

    def test_public_urls_and_malformed_urls_never_leak_authentication(self):
        result = public_value(
            {
                "endpoint": "https://user:password@example.com/api?token=secret",
                "malformed": "http://[",
            }
        )
        self.assertEqual(
            result, {"endpoint": "[configured URL]", "malformed": "[configured URL]"}
        )


class SetupStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.current = copy.deepcopy(DEFAULTS)
        self.started = []
        self.applied = []
        self.inspected = []

        def inspect(action, payload, operation):
            self.inspected.append(action["id"])
            return (
                {"execution": "already_present", "verification": "passed"}
                if action["action"] == "runtime"
                else {"execution": "waiting_input", "verification": "unavailable"}
            )

        def apply(action, payload, operation):
            self.applied.append(action["id"])
            return {
                "execution": "waiting_input",
                "verification": "not_checked",
                "reason": "Complete the selected input.",
            }

        self.manager = SetupCoordinator(
            self.temporary.name,
            configuration=lambda: self.current,
            defaults=lambda: DEFAULTS,
            start_services=lambda payload: self.started.append(payload),
            inspect_action=inspect,
            apply_action=apply,
            autostart=False,
        )
        self.addCleanup(self.manager.shutdown)

    def _run(self):
        plan = self.manager.plan(request())
        run = self.manager.start(plan["plan_id"], "request-example-1")
        self.manager.run_pending(run["run_id"])
        return self.manager.get_run(run["run_id"])

    def test_start_is_idempotent_and_manual_tasks_survive_navigation(self):
        run = self._run()
        duplicate = self.manager.start(run["plan_id"], "request-example-1")
        self.assertEqual(run["run_id"], duplicate["run_id"])
        self.assertEqual(len(self.started), 1)
        self.assertEqual(run["status"], "waiting_input")
        self.assertEqual(self.manager.latest()["run_id"], run["run_id"])
        self.assertGreater(run["coverage"]["waiting_input"], 0)

    def test_read_only_recheck_does_not_reapply_or_restart_services(self):
        run = self._run()
        applied = list(self.applied)
        self.manager.operate(run["run_id"], "recheck")
        self.manager.run_pending(run["run_id"])
        self.assertEqual(self.applied, applied)
        self.assertEqual(len(self.started), 1)

    def test_required_deferral_does_not_shrink_coverage(self):
        run = self._run()
        account = next(a for a in run["actions"] if a["action"] == "account")
        result = self.manager.operate(run["run_id"], "defer", action_id=account["id"])
        self.assertEqual(
            result["coverage"]["denominator"], run["coverage"]["denominator"]
        )
        self.assertEqual(
            next(a for a in result["actions"] if a["id"] == account["id"])["execution"],
            "waiting_input",
        )

    def test_retry_does_not_repeat_successful_runtime_installations(self):
        run = self._run()
        self.manager.operate(run["run_id"], "retry")
        self.manager.run_pending(run["run_id"])
        self.assertEqual(len(self.started), 1)

    def test_changed_plan_cannot_reuse_idempotency_identifier(self):
        run = self._run()
        other = self.manager.plan(
            {"core_services": [], "optional_services": ["audiobookshelf"]}
        )
        with self.assertRaises(SetupConflict):
            self.manager.start(other["plan_id"], run["idempotency_key"])

    def test_other_active_run_is_not_recovered_or_started_concurrently(self):
        first = self.manager.plan(request())
        run = self.manager.start(first["plan_id"], "first-active-request")
        other = SetupCoordinator(
            self.temporary.name,
            configuration=lambda: self.current,
            defaults=lambda: DEFAULTS,
            start_services=lambda _: None,
            inspect_action=lambda *_: None,
            autostart=False,
        )
        self.addCleanup(other.shutdown)
        self.assertEqual(other.get_run(run["run_id"])["status"], "pending")
        with self.assertRaises(SetupConflict):
            other.start(first["plan_id"], "second-active-request")
        self.manager.run_pending(run["run_id"])

    def test_files_private_and_symlink_records_refused(self):
        run = self._run()
        path = Path(self.temporary.name) / f"run-{run['run_id']}.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.unlink()
        outside = Path(self.temporary.name) / "outside.json"
        outside.write_text("{}")
        path.symlink_to(outside)
        with self.assertRaises(OSError):
            self.manager.get_run(run["run_id"])
        with self.assertRaises(ValueError):
            self.manager.get_run("../escape")

    def test_private_inputs_are_not_public_api_records(self):
        payload = request()
        payload["core_services"][0]["debrid_key"] = "example-private-secret"
        plan = self.manager.plan(payload)
        public = self.manager.get_plan(plan["plan_id"])
        self.assertNotIn("example-private-secret", json.dumps(public))
        private = self.manager.store.read("inputs", plan["plan_id"])
        self.assertEqual(
            private["payload"]["core_services"][0]["debrid_key"],
            "example-private-secret",
        )


if __name__ == "__main__":
    unittest.main()
