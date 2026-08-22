import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.startup import (
    frontend_start_readiness,
    run_grouped_preinstall,
    run_migration_aware_preinstall,
    run_parallel_preinstall,
    start_control_plane_before_preinstall,
)
from utils import startup


class FrontendStartupTests(unittest.TestCase):
    def _frontend_config(self, root: Path, version: str = "1.2.0") -> dict:
        entrypoint = root / ".output" / "server" / "index.mjs"
        entrypoint.parent.mkdir(parents=True)
        entrypoint.write_text("// built frontend\n", encoding="utf-8")
        (root / "package.json").write_text(
            json.dumps({"name": "dmbdb", "version": version}),
            encoding="utf-8",
        )
        return {
            "enabled": True,
            "config_dir": str(root),
            "command": ["node", ".output/server/index.mjs"],
            "commit_sha": "",
            "branch_enabled": False,
            "release_version_enabled": False,
        }

    def test_installed_frontend_can_start_before_service_preinstall(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._frontend_config(Path(temp_dir))

            self.assertTrue(frontend_start_readiness(config)[0])

    def test_missing_runtime_is_deferred(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._frontend_config(Path(temp_dir))
            (Path(temp_dir) / ".output" / "server" / "index.mjs").unlink()

            self.assertFalse(frontend_start_readiness(config)[0])

    def test_branch_install_is_deferred(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._frontend_config(Path(temp_dir))
            config["branch_enabled"] = True

            self.assertFalse(frontend_start_readiness(config)[0])

    def test_commit_install_is_deferred(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._frontend_config(Path(temp_dir))
            config["commit_sha"] = "a" * 40

            ready, reason = frontend_start_readiness(config)

            self.assertFalse(ready)
            self.assertIn("commit installation", reason)

    def test_matching_pinned_release_can_start_early(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._frontend_config(Path(temp_dir), version="1.2.0")
            config.update(
                {"release_version_enabled": True, "release_version": "v1.2.0"}
            )

            self.assertTrue(frontend_start_readiness(config)[0])

    def test_different_or_floating_release_is_deferred(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._frontend_config(Path(temp_dir), version="1.2.0")
            config.update(
                {"release_version_enabled": True, "release_version": "v1.3.0"}
            )
            self.assertFalse(frontend_start_readiness(config)[0])

            config["release_version"] = "latest"
            self.assertFalse(frontend_start_readiness(config)[0])

    def test_installed_frontend_starts_before_service_preinstall(self):
        events = []
        with tempfile.TemporaryDirectory() as temp_dir:
            frontend_config = self._frontend_config(Path(temp_dir))
            start_control_plane_before_preinstall(
                api_enabled=True,
                start_api=lambda: events.append("api"),
                frontend_config=frontend_config,
                start_frontend=lambda: events.append("frontend") or True,
                preinstall_services=lambda: events.append("preinstall"),
            )

        self.assertEqual(events, ["api", "frontend", "preinstall"])

    def test_frontend_needing_install_starts_after_service_preinstall(self):
        events = []
        with tempfile.TemporaryDirectory() as temp_dir:
            frontend_config = self._frontend_config(Path(temp_dir))
            (Path(temp_dir) / ".output" / "server" / "index.mjs").unlink()
            start_control_plane_before_preinstall(
                api_enabled=True,
                start_api=lambda: events.append("api"),
                frontend_config=frontend_config,
                start_frontend=lambda: events.append("frontend") or True,
                preinstall_services=lambda: events.append("preinstall"),
            )

        self.assertEqual(events, ["api", "preinstall", "frontend"])

    def test_parallel_preinstall_reports_failure_without_raising(self):
        attempted = []

        def install_target(_key, name):
            attempted.append(name)
            if name == "Prowlarr":
                raise RuntimeError("archive validation failed")

        failures = run_parallel_preinstall(
            [("prowlarr", "Prowlarr"), ("sonarr", "Sonarr")],
            install_target,
        )

        self.assertCountEqual(attempted, ["Prowlarr", "Sonarr"])
        self.assertEqual(failures, {"Prowlarr": "archive validation failed"})

    def test_grouped_preinstall_serializes_instances_of_same_service(self):
        active_keys = set()
        overlap = []
        lock = threading.Lock()

        def install_target(key, _name):
            with lock:
                if key in active_keys:
                    overlap.append(key)
                active_keys.add(key)
            time.sleep(0.01)
            with lock:
                active_keys.remove(key)

        failures = run_grouped_preinstall(
            [
                ("seerr", "Seerr Main"),
                ("seerr", "Seerr InfiniDysk"),
                ("sonarr", "Sonarr Main"),
            ],
            install_target,
        )

        self.assertEqual(failures, {})
        self.assertEqual(overlap, [])

    def test_grouped_preinstall_uses_dynamic_resource_limit(self):
        targets = [(f"service-{index}", f"Service {index}") for index in range(10)]
        with patch.object(
            startup, "preinstall_worker_count", return_value=6
        ) as worker_count:
            failures = run_grouped_preinstall(targets, lambda _key, _name: None)

        self.assertEqual(failures, {})
        worker_count.assert_called_once_with(10)

    def test_pre_mutation_namespace_recovery_skips_runtime_preinstall(self):
        attempted = []
        with patch(
            "utils.infinidysk_migration_admission."
            "infinidysk_namespace_pre_mutation_interrupted",
            return_value=True,
        ):
            failures, skipped_reason = run_migration_aware_preinstall(
                [("infinidysk", "InfiniDysk"), ("sonarr", "Sonarr")],
                lambda key, name: attempted.append((key, name)),
            )

        self.assertEqual(failures, {})
        self.assertEqual(attempted, [])
        self.assertIn("already-installed legacy topology", skipped_reason)

    def test_normal_boot_still_runs_grouped_preinstall(self):
        attempted = []
        with patch(
            "utils.infinidysk_migration_admission."
            "infinidysk_namespace_pre_mutation_interrupted",
            return_value=False,
        ):
            failures, skipped_reason = run_migration_aware_preinstall(
                [("infinidysk", "InfiniDysk")],
                lambda key, name: attempted.append((key, name)),
            )

        self.assertEqual(failures, {})
        self.assertIsNone(skipped_reason)
        self.assertEqual(attempted, [("infinidysk", "InfiniDysk")])


if __name__ == "__main__":
    unittest.main()
