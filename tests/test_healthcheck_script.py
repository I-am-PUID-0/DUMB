import unittest
from unittest.mock import patch

import healthcheck


class HealthcheckStartupLifecycleTests(unittest.TestCase):
    def test_nonterminal_startup_phase_is_not_reported_unhealthy(self):
        with (
            patch.object(
                healthcheck,
                "load_startup_state",
                return_value={"phase": "stabilizing"},
            ),
            patch.object(healthcheck, "load_running_processes") as running,
            self.assertRaises(SystemExit) as exit_error,
        ):
            healthcheck.main()

        self.assertEqual(exit_error.exception.code, 0)
        running.assert_not_called()

    def test_degraded_startup_is_reported_unhealthy(self):
        with (
            patch.object(
                healthcheck,
                "load_startup_state",
                return_value={
                    "phase": "degraded",
                    "failures": {"Example": "Port is not responding"},
                },
            ),
            patch.object(healthcheck, "load_running_processes", return_value={}),
            self.assertRaises(SystemExit) as exit_error,
        ):
            healthcheck.main()

        self.assertEqual(exit_error.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
