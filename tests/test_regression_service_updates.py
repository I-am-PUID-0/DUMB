import argparse
import json
import tempfile
import unittest
from pathlib import Path

from scripts import regression_service_updates as suite


class RegressionServiceUpdatesTests(unittest.TestCase):
    def _matrix(self, root: Path) -> Path:
        path = root / "matrix.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "cases": [
                        {
                            "id": "ready",
                            "key": "pulsarr",
                            "previous": "v1",
                            "qualified": True,
                        },
                        {
                            "id": "pending",
                            "key": "bazarr",
                            "previous": "v1",
                            "qualified": False,
                        },
                        {"id": "missing", "key": "plex", "qualified": False},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_load_cases_defaults_to_qualified_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runnable, skipped = suite.load_cases(self._matrix(Path(temp_dir)))

        self.assertEqual(["ready"], [case["id"] for case in runnable])
        self.assertEqual({"pending", "missing"}, {case["id"] for case in skipped})

    def test_load_cases_can_include_pending_entries_with_versions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runnable, skipped = suite.load_cases(
                self._matrix(Path(temp_dir)), include_pending=True
            )

        self.assertEqual({"ready", "pending"}, {case["id"] for case in runnable})
        self.assertEqual(["missing"], [case["id"] for case in skipped])

    def test_case_command_forwards_instance_dependencies_and_timeouts(self):
        args = argparse.Namespace(
            image="image:test",
            cache_dir=Path("/cache"),
            startup_timeout=100,
            update_timeout=200,
            keep=False,
            worker_env={},
        )
        command = suite.case_command(
            {
                "key": "seerr",
                "instance": "Default",
                "previous": "v1",
                "target_version": "prerelease",
                "update_target": "channel",
                "selector": "release",
                "mode": "install-only",
                "dependencies": ["postgres"],
                "health_urls": ["http://127.0.0.1:3000/healthz"],
                "config_overrides": {"seerr": {"regression": True}},
                "startup_timeout": 300,
            },
            args,
        )

        self.assertIn("Default", command)
        self.assertEqual("prerelease", command[command.index("--target-version") + 1])
        self.assertEqual("channel", command[command.index("--update-target") + 1])
        self.assertIn("postgres", command)
        self.assertIn("--config-overrides-json", command)
        self.assertEqual(
            "http://127.0.0.1:3000/healthz",
            command[command.index("--health-url") + 1],
        )
        self.assertEqual("install-only", command[command.index("--mode") + 1])
        overrides = json.loads(command[command.index("--config-overrides-json") + 1])
        self.assertTrue(overrides["seerr"]["regression"])
        self.assertEqual("300", command[command.index("--startup-timeout") + 1])
        self.assertEqual("200", command[command.index("--update-timeout") + 1])

    def test_extract_report_accepts_final_json_object(self):
        report = suite._extract_report('progress\n{"result": "passed"}\n')
        self.assertEqual("passed", report["result"])

    def test_interrupt_cancels_queued_cases_before_stopping_workers(self):
        source = Path("scripts/regression_service_updates.py").read_text(
            encoding="utf-8"
        )
        cancel_index = source.index("future.cancel()")
        stop_index = source.index("stop_active_workers()", cancel_index)

        self.assertLess(cancel_index, stop_index)


if __name__ == "__main__":
    unittest.main()
