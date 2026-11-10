import copy
from contextlib import contextmanager
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest

from utils.arrangement_state import (
    SetupCoordinator,
    SetupStore,
    SetupConflict,
    _merge_inputs,
)
from tests.test_arrangement_plan_state import DEFAULTS

PAYLOAD = {"core_services": [], "optional_services": ["audiobookshelf"]}


def _active_child(directory, plan_id, output, release):
    manager = SetupCoordinator(
        directory,
        configuration=lambda: copy.deepcopy(DEFAULTS),
        defaults=lambda: DEFAULTS,
        start_services=lambda payload: None,
        inspect_action=lambda action, payload, operation: {
            "execution": "already_present",
            "verification": "passed",
        },
        autostart=False,
    )
    try:
        record = manager.start(plan_id, "cross-process-stable-request")
        output.put(record["run_id"])
        release.wait(10)
        manager.run_pending(record["run_id"])
    finally:
        manager.shutdown()


class SetupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tick = [1000.0]
        self.calls = []
        self.starts = []
        self.managers = []

    def manager(self, **kwargs):
        options = dict(
            configuration=lambda: copy.deepcopy(DEFAULTS),
            defaults=lambda: DEFAULTS,
            start_services=lambda payload: self.starts.append(copy.deepcopy(payload)),
            inspect_action=lambda action, payload, operation: (
                {"execution": "already_present", "verification": "passed"}
                if action["action"] == "runtime"
                else {"execution": "waiting_input", "verification": "unavailable"}
            ),
            apply_action=lambda action, payload, operation: {
                "execution": "waiting_input",
                "verification": "unavailable",
            },
            autostart=False,
            clock=lambda: self.tick[0],
            retry_base_seconds=5,
        )
        options.update(kwargs)
        manager = SetupCoordinator(self.temp.name, **options)
        self.managers.append(manager)
        self.addCleanup(manager.shutdown)
        return manager

    def run_new(self, manager):
        plan = manager.plan(copy.deepcopy(PAYLOAD))
        run = manager.start(plan["plan_id"], "request-example-stable")
        manager.run_pending(run["run_id"])
        return manager.get_run(run["run_id"])

    def test_durable_bounded_retry_never_restarts_successful_installers(self):
        def apply(action, payload, operation):
            if action["action"] == "libraries":
                self.calls.append(action["id"])
                return {
                    "execution": "failed",
                    "verification": "not_checked",
                    "retryable": True,
                }
            return {"execution": "waiting_input", "verification": "unavailable"}

        manager = self.manager(apply_action=apply, retry_limit=2)
        run = self.run_new(manager)
        target = next(item for item in run["actions"] if item["action"] == "libraries")
        self.assertEqual(target["attempts"], 1)
        self.assertTrue(target["next_retry_at"])
        self.assertIsNone(manager.process_due_retries())
        self.tick[0] += 6
        identifier = manager.process_due_retries()
        self.assertEqual(identifier, run["run_id"])
        manager.run_pending(identifier)
        target = next(
            item
            for item in manager.get_run(identifier)["actions"]
            if item["action"] == "libraries"
        )
        self.assertEqual(target["attempts"], 2)
        self.assertIsNone(target["next_retry_at"])
        self.assertTrue(target["retry_exhausted"])
        self.assertEqual(len(self.starts), 1)
        self.tick[0] += 1000
        self.assertIsNone(manager.process_due_retries())

    def test_cancel_before_worker_start_does_not_invoke_installer(self):
        manager = self.manager()
        plan = manager.plan(copy.deepcopy(PAYLOAD))
        run = manager.start(plan["plan_id"], "cancel-before-start")
        manager.operate(run["run_id"], "cancel")
        manager.run_pending(run["run_id"])
        self.assertEqual(self.starts, [])
        self.assertEqual(manager.get_run(run["run_id"])["status"], "interrupted")

    def test_mutation_guard_covers_installer_and_action_lifetime(self):
        held = []

        @contextmanager
        def guard():
            held.append(True)
            try:
                yield
            finally:
                held.pop()

        def start(payload):
            self.assertEqual(held, [True])

        def apply(action, payload, operation):
            self.assertEqual(held, [True])
            return {"execution": "waiting_input"}

        manager = self.manager(
            mutation_guard=guard, start_services=start, apply_action=apply
        )
        self.run_new(manager)
        self.assertFalse(held)

    def test_corrupt_run_does_not_prevent_recovery_or_latest(self):
        identifier = "f" * 32
        file = Path(self.temp.name) / f"run-{identifier}.json"
        file.write_text("{malformed")
        file.chmod(0o600)
        manager = self.manager()
        run = self.run_new(manager)
        self.assertEqual(manager.latest()["run_id"], run["run_id"])
        self.assertEqual(file.read_text(), "{malformed")

    def test_private_minted_values_flow_to_later_actions_without_public_leak(self):
        observed = []

        def apply(action, payload, operation):
            if action["action"] == "account":
                return {
                    "execution": "applied",
                    "verification": "passed",
                    "_private_values": {
                        "audiobookshelf::api_key": "minted-fixture-key"
                    },
                    "_clear_inputs": ["audiobookshelf::admin_password"],
                }
            observed.append(copy.deepcopy(payload))
            return {"execution": "waiting_input"}

        manager = self.manager(apply_action=apply)
        payload = copy.deepcopy(PAYLOAD)
        payload["arrangement"] = {
            "values": {"audiobookshelf::admin_password": "fixture-first-password"}
        }
        plan = manager.plan(payload)
        run = manager.start(plan["plan_id"], "minted-values-request")
        manager.run_pending(run["run_id"])
        private = manager.store.read("inputs", plan["plan_id"])["payload"][
            "arrangement"
        ]["values"]
        self.assertEqual(private["audiobookshelf::api_key"], "minted-fixture-key")
        self.assertNotIn("audiobookshelf::admin_password", private)
        self.assertEqual(
            observed[-1]["arrangement"]["values"]["audiobookshelf::api_key"],
            "minted-fixture-key",
        )
        public = json.dumps(manager.get_run(run["run_id"]))
        self.assertNotIn("minted-fixture-key", public)
        self.assertNotIn("fixture-first-password", public)

    def test_blank_secret_edit_preserves_saved_value_and_invalidates_prior_evidence(
        self,
    ):
        manager = self.manager()
        payload = copy.deepcopy(PAYLOAD)
        payload["arrangement"] = {
            "values": {"audiobookshelf::api_key": "fixture-old-key"}
        }
        plan = manager.plan(payload)
        run = manager.start(plan["plan_id"], "blank-edit-request")
        manager.run_pending(run["run_id"])
        target = next(
            item
            for item in manager.get_run(run["run_id"])["actions"]
            if item["action"] == "libraries"
        )
        before = manager.get_run(run["run_id"])["fingerprint"]
        manager.operate(
            run["run_id"],
            "apply",
            action_id=target["id"],
            values={
                "audiobookshelf::api_key": "",
                "audiobookshelf::libraries": [
                    {"name": "Books", "media_type": "book", "paths": ["/library/books"]}
                ],
            },
        )
        raw = manager.store.read("inputs", plan["plan_id"])["payload"]["arrangement"][
            "values"
        ]
        self.assertEqual(raw["audiobookshelf::api_key"], "fixture-old-key")
        self.assertNotEqual(manager.get_run(run["run_id"])["fingerprint"], before)
        manager.run_pending(run["run_id"])

    def test_configured_marker_is_never_accepted_as_a_secret(self):
        manager = self.manager()
        payload = copy.deepcopy(PAYLOAD)
        payload["arrangement"] = {"values": {"audiobookshelf::api_key": "[configured]"}}
        with self.assertRaises(ValueError):
            manager.plan(payload)

    def test_cross_process_duplicate_request_reuses_run_and_cancel_is_not_lost(self):
        manager = self.manager()
        plan = manager.plan(copy.deepcopy(PAYLOAD))
        context = multiprocessing.get_context("spawn")
        output, release = context.Queue(), context.Event()
        child = context.Process(
            target=_active_child,
            args=(self.temp.name, plan["plan_id"], output, release),
        )
        child.start()
        try:
            identifier = output.get(timeout=8)
            duplicate = manager.start(plan["plan_id"], "cross-process-stable-request")
            self.assertEqual(duplicate["run_id"], identifier)
            with self.assertRaises(SetupConflict):
                manager.start(plan["plan_id"], "different-process-request")
            manager.operate(identifier, "cancel")
            release.set()
            child.join(timeout=8)
            self.assertEqual(child.exitcode, 0)
            self.assertEqual(manager.get_run(identifier)["status"], "interrupted")
            self.assertEqual(self.starts, [])
        finally:
            release.set()
            child.join(timeout=3)
            if child.is_alive():
                child.terminate()
                child.join(timeout=3)
            output.close()
            output.join_thread()

    def test_retry_schedule_survives_controller_restart(self):
        def apply(action, payload, operation):
            return (
                {"execution": "failed", "retryable": True}
                if action["action"] == "libraries"
                else {"execution": "waiting_input"}
            )

        first = self.manager(apply_action=apply)
        run = self.run_new(first)
        first.shutdown()
        self.tick[0] += 6
        second = self.manager(apply_action=apply)
        identifier = second.process_due_retries()
        self.assertEqual(identifier, run["run_id"])
        second.run_pending(identifier)
        target = next(
            item
            for item in second.get_run(identifier)["actions"]
            if item["action"] == "libraries"
        )
        self.assertEqual(target["attempts"], 2)
        self.assertEqual(len(self.starts), 1)

    def test_credential_merge_never_relays_saved_account_to_another_nntp_host(self):
        key = "infinidysk::nntp"
        fields = [{"key": key, "type": "nntp_providers"}]
        old = {
            key: [
                {
                    "host": "news.example.com",
                    "port": 563,
                    "username": "fixture-user",
                    "password": "fixture-pass",
                }
            ]
        }
        retained = _merge_inputs(
            old,
            {
                key: [
                    {
                        "host": "news.example.com",
                        "port": 563,
                        "username": "",
                        "password": "",
                    }
                ]
            },
            fields,
        )
        self.assertEqual(retained, old)
        moved = _merge_inputs(
            old,
            {
                key: [
                    {
                        "host": "other.example.com",
                        "port": 563,
                        "username": "",
                        "password": "",
                    }
                ]
            },
            fields,
        )
        self.assertEqual(moved[key][0]["password"], "")
        ambiguous = {key: old[key] + [{**old[key][0], "username": "other-user"}]}
        with self.assertRaises(ValueError):
            _merge_inputs(
                ambiguous, {key: [{"host": "news.example.com", "port": 563}]}, fields
            )

    def test_terminal_history_retention_preserves_unfinished_runs_and_inputs(self):
        manager = self.manager(retention_runs=1, retention_days=1)
        pending = self.run_new(manager)
        completed = []
        for index in range(3):
            self.tick[0] += 1
            plan = manager.plan(copy.deepcopy(PAYLOAD))
            record = manager.start(plan["plan_id"], f"history-request-{index}")
            manager.run_pending(record["run_id"])
            saved = manager.get_run(record["run_id"])
            saved["status"] = "complete"
            manager.store.write("run", record["run_id"], saved)
            completed.append(saved["run_id"])
        with manager._transaction():
            manager._prune()
        self.assertEqual(manager.get_run(pending["run_id"])["status"], "waiting_input")
        self.assertTrue(manager.store.read("inputs", pending["plan_id"]))
        self.assertTrue(manager.get_run(completed[-1]))
        with self.assertRaisesRegex(FileNotFoundError, "expired"):
            manager.get_run(completed[0])

    def test_guard_failure_never_leaves_run_running(self):
        @contextmanager
        def guard():
            raise SetupConflict("fixture reservation")
            yield

        manager = self.manager(mutation_guard=guard)
        run = self.run_new(manager)
        self.assertEqual(run["status"], "interrupted")
        self.assertEqual(self.starts, [])
        self.assertFalse(
            any(item.get("execution") == "running" for item in run["actions"])
        )

    def test_schema_refresh_advertises_new_fields_without_shrinking_resources(self):
        ready = [False]

        def discover(resource, config, payload):
            if not ready[0]:
                return []
            return [
                {
                    "key": resource["id"] + "::profile_choice",
                    "type": "select",
                    "label": "Profile",
                    "options": [{"value": 7, "label": "Chosen"}],
                }
            ]

        def start(payload):
            ready[0] = True
            self.starts.append(payload)

        manager = self.manager(discover_fields=discover, start_services=start)
        run = self.run_new(manager)
        self.assertIn(
            "audiobookshelf::profile_choice",
            {field["key"] for field in run["arrangement_schema"]["fields"]},
        )
        services = copy.deepcopy(run["services"])
        target = next(item for item in run["actions"] if item["action"] == "libraries")
        manager.operate(
            run["run_id"],
            "recheck",
            action_id=target["id"],
            values={"audiobookshelf::profile_choice": 7},
        )
        manager.run_pending(run["run_id"])
        self.assertEqual(manager.get_run(run["run_id"])["services"], services)
        self.assertEqual(manager.get_run(run["run_id"])["revision"], 2)

    def test_store_rejects_excessive_depth_shared_records_and_hardlinks(self):
        store = SetupStore(self.temp.name)
        self.addCleanup(store.close)
        deeply = {}
        current = deeply
        for _ in range(80):
            current["nested"] = {}
            current = current["nested"]
        with self.assertRaises(ValueError):
            store.write("plan", "a" * 32, deeply)
        original = Path(self.temp.name) / "source.json"
        original.write_text("{}")
        original.chmod(0o600)
        os.link(original, Path(self.temp.name) / ("run-" + "b" * 32 + ".json"))
        with self.assertRaises(PermissionError):
            store.read("run", "b" * 32)

    def test_topology_inputs_require_a_new_plan_without_mutating_saved_inputs(self):
        manager = self.manager()
        payload = {"core_services": [{"name": "cli_debrid"}], "optional_services": []}
        plan = manager.plan(payload)
        run = manager.start(plan["plan_id"], "immutable-topology-request")
        manager.run_pending(run["run_id"])
        original = manager.store.read("inputs", plan["plan_id"])
        for key, value in (("battery_mode", "standalone"), ("use_phalanx_db", True)):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ValueError, "Create a new plan"),
            ):
                manager.operate(
                    run["run_id"], "apply", values={f"cli_debrid::{key}": value}
                )
            self.assertEqual(manager.store.read("inputs", plan["plan_id"]), original)

    def test_shared_input_evidence_invalidation_follows_transitive_references(self):
        manager = self.manager()
        run = self.run_new(manager)
        # Put the furthest consumer first: a single traversal misses C -> B -> A.
        with manager._transaction():
            record = manager.get_run(run["run_id"])
            private = manager.store.read("inputs", record["plan_id"])
            private["payload"]["arrangement"] = {
                "values": {
                    "alphaConsumer::nntp_source": "zuluConsumer",
                    "zuluConsumer::nntp_source": "audiobookshelf",
                }
            }
            manager.store.write("inputs", record["plan_id"], private)
            record["arrangement_schema"]["fields"].append(
                {
                    "key": "audiobookshelf::choice",
                    "resource_id": "audiobookshelf",
                    "type": "text",
                    "label": "Choice",
                }
            )
            original = next(
                item for item in record["actions"] if item["action"] == "libraries"
            )
            for source in ("alphaConsumer", "zuluConsumer"):
                record["actions"].append(
                    {
                        **copy.deepcopy(original),
                        "id": source + "::connection",
                        "source": source,
                        "target": None,
                        "execution": "applied",
                        "verification": "passed",
                        "evidence": {"level": "fixture"},
                    }
                )
            manager.store.write("run", record["run_id"], record)
        manager.operate(
            run["run_id"],
            "apply",
            action_id=original["id"],
            values={"audiobookshelf::choice": "updated"},
        )
        consumers = [
            item
            for item in manager.get_run(run["run_id"])["actions"]
            if item["source"] in {"zuluConsumer", "alphaConsumer"}
        ]
        self.assertEqual(len(consumers), 2)
        self.assertTrue(
            all(
                item["verification"] == "stale" and item["evidence"] is None
                for item in consumers
            )
        )
        manager.run_pending(run["run_id"])

    def test_unknown_capabilities_remain_in_denominator_and_can_be_attested(self):
        manager = self.manager()
        run = self.run_new(manager)
        target = next(item for item in run["actions"] if item["action"] == "libraries")
        # Start with a supported injected adapter, independently of the
        # installed-source capability defaults in the evolving catalog.
        manager._save_action(run["run_id"], target["id"], {"automation": "after_input"})
        baseline = manager.get_run(run["run_id"])
        manager._finish(baseline)
        before = baseline["coverage"]
        manager._save_action(
            run["run_id"],
            target["id"],
            {
                "automation": "unknown",
                "execution": "waiting_input",
                "verification": "unavailable",
            },
        )
        changed = manager.get_run(run["run_id"])
        with manager._transaction():
            manager._finish(changed)
            manager.store.write("run", run["run_id"], changed)
        self.assertEqual(changed["coverage"]["denominator"], before["denominator"])
        self.assertEqual(changed["coverage"]["unknown"], before["unknown"] + 1)
        self.assertEqual(changed["coverage"]["after_input"], before["after_input"] - 1)
        manager.operate(run["run_id"], "confirm", action_id=target["id"])
        manager.operate(run["run_id"], "retry")
        manager.run_pending(run["run_id"])
        confirmed = next(
            item
            for item in manager.get_run(run["run_id"])["actions"]
            if item["id"] == target["id"]
        )
        self.assertEqual(confirmed["evidence"]["level"], "user_attested")
        self.assertEqual(confirmed["verification"], "unavailable")
        # An explicit recheck is permitted to replace a human assertion.
        manager.operate(run["run_id"], "recheck", action_id=target["id"])
        manager.run_pending(run["run_id"])
        checked = next(
            item
            for item in manager.get_run(run["run_id"])["actions"]
            if item["id"] == target["id"]
        )
        self.assertEqual(checked["execution"], "waiting_input")

    def test_invalid_automation_classification_is_not_saved(self):
        manager = self.manager()
        run = self.run_new(manager)
        target = run["actions"][0]
        manager._save_action(
            run["run_id"], target["id"], {"automation": "fixture-unknown-enum"}
        )
        self.assertEqual(
            manager.get_run(run["run_id"])["actions"][0]["automation"],
            target["automation"],
        )

    def test_partial_runtime_retry_carries_only_failed_resource_scope_ephemerally(self):
        def inspect(action, payload, operation):
            if action["action"] != "runtime":
                return {"execution": "waiting_input", "verification": "unavailable"}
            if action["source"] == "plex" and len(self.starts) == 1:
                return {"execution": "failed", "verification": "failed"}
            return {"execution": "already_present", "verification": "passed"}

        manager = self.manager(inspect_action=inspect)
        payload = {
            "core_services": [{"name": "plex"}],
            "optional_services": ["audiobookshelf"],
        }
        plan = manager.plan(payload)
        run = manager.start(plan["plan_id"], "partial-runtime-retry")
        manager.run_pending(run["run_id"])
        manager.operate(run["run_id"], "retry")
        manager.run_pending(run["run_id"])
        self.assertEqual(len(self.starts), 2)
        self.assertEqual(self.starts[1]["_retry_resources"], ["plex"])
        self.assertNotIn("_retry_resources", self.starts[0])
        self.assertNotIn("_retry_resources", payload)
        self.assertNotIn(
            "_retry_resources", manager.store.read("inputs", plan["plan_id"])["payload"]
        )

    def test_private_handoff_clears_ephemeral_password_only_with_minted_credential(
        self,
    ):
        for minted in (False, True):
            with self.subTest(minted=minted):

                def apply(action, payload, operation):
                    if action["action"] != "account":
                        return {"execution": "waiting_input"}
                    return {
                        "execution": "waiting_dependency",
                        "verification": "not_checked",
                        "_private_values": (
                            {"audiobookshelf::api_key": "fixture-minted-session"}
                            if minted
                            else {}
                        ),
                        "_clear_inputs": ["audiobookshelf::admin_password"],
                        "_clear_on_handoff": True,
                    }

                manager = self.manager(apply_action=apply)
                payload = copy.deepcopy(PAYLOAD)
                payload["arrangement"] = {
                    "values": {
                        "audiobookshelf::admin_password": "fixture-once-password"
                    }
                }
                plan = manager.plan(payload)
                run = manager.start(plan["plan_id"], "private-handoff-" + str(minted))
                manager.run_pending(run["run_id"])
                values = manager.store.read("inputs", plan["plan_id"])["payload"][
                    "arrangement"
                ]["values"]
                if minted:
                    self.assertNotIn("audiobookshelf::admin_password", values)
                    self.assertEqual(
                        values["audiobookshelf::api_key"], "fixture-minted-session"
                    )
                else:
                    self.assertEqual(
                        values["audiobookshelf::admin_password"],
                        "fixture-once-password",
                    )
                public = json.dumps(manager.get_run(run["run_id"]))
                self.assertNotIn("fixture-once-password", public)
                self.assertNotIn("fixture-minted-session", public)
                manager.shutdown()

    def test_each_accepted_plan_has_one_run_and_new_plan_inputs_are_isolated(self):
        manager = self.manager()
        first_payload = copy.deepcopy(PAYLOAD)
        first_payload["arrangement"] = {
            "values": {"audiobookshelf::api_key": "fixture-first-key"}
        }
        first_plan = manager.plan(first_payload)
        first = manager.start(first_plan["plan_id"], "single-plan-original-request")
        manager.run_pending(first["run_id"])
        self.assertEqual(
            manager.start(first_plan["plan_id"], "single-plan-original-request")[
                "run_id"
            ],
            first["run_id"],
        )
        with self.assertRaisesRegex(SetupConflict, "Create a new plan"):
            manager.start(first_plan["plan_id"], "single-plan-different-request")
        self.assertEqual(len(manager.store.records("run")), 1)

        second_payload = copy.deepcopy(PAYLOAD)
        second_payload["arrangement"] = {
            "values": {"audiobookshelf::api_key": "fixture-second-key"}
        }
        second_plan = manager.plan(second_payload)
        second = manager.start(second_plan["plan_id"], "separate-plan-second-request")
        manager.run_pending(second["run_id"])
        before = manager.get_run(second["run_id"])
        target = next(
            item
            for item in manager.get_run(first["run_id"])["actions"]
            if item["action"] == "libraries"
        )
        manager.operate(
            first["run_id"],
            "apply",
            action_id=target["id"],
            values={"audiobookshelf::api_key": "fixture-updated-old-key"},
        )
        manager.run_pending(first["run_id"])
        private_second = manager.store.read("inputs", second_plan["plan_id"])[
            "payload"
        ]["arrangement"]["values"]
        self.assertEqual(
            private_second["audiobookshelf::api_key"], "fixture-second-key"
        )
        self.assertEqual(
            manager.get_run(second["run_id"])["revision"], before["revision"]
        )
        self.assertEqual(
            manager.get_run(second["run_id"])["fingerprint"], before["fingerprint"]
        )
