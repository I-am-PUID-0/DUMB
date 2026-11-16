"""Real manager scope and running/recovery status regressions."""

import threading
import unittest

import tests.test_infinidysk_v2_migration as fixtures


class ScopeStatusTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InfiniDyskV2MigrationTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_enabled_bazarr_is_a_managed_consumer_but_disabled_is_not(self):
        fixture = self.fixture
        fixture.config.config["bazarr"] = {
            "instances": {
                "active": {"enabled": True, "process_name": "Bazarr Movies"},
                "inactive": {"enabled": False, "process_name": "Bazarr Disabled"},
            }
        }
        inventory = fixture.manager._inventory(
            fixture.config.config, fixture.config.config["infinidysk"]
        )
        self.assertEqual(
            [("bazarr", "active", "Bazarr Movies")],
            [
                (item["service_key"], item["instance"], item["process_name"])
                for item in inventory["consumers"]
            ],
        )

    def test_running_rehearsal_does_not_claim_recovery_is_required(self):
        fixture = self.fixture
        fixture.executor.gate = threading.Event()
        _, request = fixture.preview_request()
        response = fixture.manager.rehearse(request)
        self.assertTrue(fixture.executor.started.wait(timeout=2))
        status = fixture.manager.status()
        self.assertEqual(response["job"]["job_id"], status["job"]["job_id"])
        self.assertFalse(status["eligibility"]["ready"])
        self.assertEqual(1, len(status["eligibility"]["blockers"]))
        self.assertIn("already running", status["eligibility"]["blockers"][0])
        self.assertNotIn("recovery", status["eligibility"]["blockers"][0])
        fixture.executor.gate.set()
        fixture.wait(response)

    def test_recovery_hold_is_not_described_as_running(self):
        fixture = self.fixture
        fixture.executor.skipped = 1
        fixture.executor.fail_recovery = True
        _, request = fixture.preview_request()
        fixture.wait(fixture.manager.rehearse(request))
        status = fixture.manager.status()
        self.assertEqual("recovery_required", status["job"]["status"])
        self.assertEqual(
            ["A previous migration needs recovery before another can start."],
            status["eligibility"]["blockers"],
        )
