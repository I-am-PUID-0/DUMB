import copy
import json
import logging
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from utils.arrangement_plan import build_plan
from utils.arrangement_runtime import ArrangementRuntime
from utils.arrangement_state import SetupCoordinator

DEFAULTS = json.loads(
    (Path(__file__).resolve().parents[1] / "utils/dumb_config.json").read_text()
)


class RivenSetupContractTests(unittest.TestCase):
    def payload(self):
        return {"core_services": [{"name": "riven_backend"}]}

    def test_standalone_plan_has_settings_and_optional_media_without_frontend(self):
        plan = build_plan(self.payload(), DEFAULTS, DEFAULTS, plan_id="c" * 32)
        self.assertNotIn(
            "riven_frontend", {item["service_key"] for item in plan["services"]}
        )
        actions = {
            item["action"]: item
            for item in plan["actions"]
            if item["source"] == "riven_backend"
        }
        self.assertTrue(actions["backend_connection"]["required"])
        self.assertFalse(actions["media_connection"]["required"])
        self.assertIn(
            "riven_backend::runtime", actions["backend_connection"]["prerequisites"]
        )
        self.assertEqual(
            actions["backend_connection"]["input_fields"], ["riven_backend::api_key"]
        )
        fields = {item["key"]: item for item in plan["arrangement_schema"]["fields"]}
        self.assertEqual(fields["riven_backend::api_key"]["type"], "password")
        self.assertTrue(fields["riven_backend::adoption_preview_id"]["internal"])

    def test_runtime_routes_both_riven_actions_to_the_specific_adapter(self):
        manager = types.SimpleNamespace(config=copy.deepcopy(DEFAULTS))
        runtime = ArrangementRuntime(
            manager, Mock(), Mock(), logging.getLogger(__name__)
        )
        payload = self.payload()
        for name in ("backend_connection", "media_connection"):
            for read_only in (True, False):
                with self.subTest(name=name, read_only=read_only):
                    action = {
                        "source": "riven_backend",
                        "service_key": "riven_backend",
                        "action": name,
                    }
                    expected = {
                        "execution": "already_present",
                        "verification": "passed",
                    }
                    with (
                        patch(
                            "utils.arrangement_riven.inspect_riven",
                            return_value=expected,
                        ) as inspect,
                        patch(
                            "utils.arrangement_riven.apply_riven", return_value=expected
                        ) as apply,
                    ):
                        self.assertEqual(
                            runtime._dispatch(
                                action,
                                payload,
                                "inspect" if read_only else "apply",
                                read_only=read_only,
                            ),
                            expected,
                        )
                        called = inspect if read_only else apply
                        called.assert_called_once_with(
                            action,
                            manager.config,
                            payload,
                            "inspect" if read_only else "apply",
                        )
                        (apply if read_only else inspect).assert_not_called()

    def test_already_present_key_handoff_is_private_and_retained_without_frontend(self):
        with tempfile.TemporaryDirectory() as directory:

            def inspect(action, payload, operation):
                return {"execution": "already_present", "verification": "passed"}

            def apply(action, payload, operation):
                result = inspect(action, payload, operation)
                if action["id"] == "riven_backend::backend_connection":
                    result["_private_values"] = {
                        "riven_backend::api_key": "private-generated-riven-key"
                    }
                return result

            coordinator = SetupCoordinator(
                directory,
                configuration=lambda: DEFAULTS,
                defaults=lambda: DEFAULTS,
                start_services=lambda payload: None,
                inspect_action=inspect,
                apply_action=apply,
                autostart=False,
            )
            self.addCleanup(coordinator.shutdown)
            plan = coordinator.plan(self.payload())
            run = coordinator.start(plan["plan_id"], "standalone-riven-handoff")
            coordinator.run_pending(run["run_id"])
            private = coordinator.store.read("inputs", plan["plan_id"])["payload"][
                "arrangement"
            ]["values"]
            self.assertEqual(
                private["riven_backend::api_key"], "private-generated-riven-key"
            )
            self.assertNotIn(
                "private-generated-riven-key",
                json.dumps(coordinator.get_run(run["run_id"])),
            )
            self.assertNotIn(
                "private-generated-riven-key", json.dumps(coordinator.latest())
            )


if __name__ == "__main__":
    unittest.main()
