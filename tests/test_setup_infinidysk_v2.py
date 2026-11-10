"""Private setup plans must delegate native transitions and preserve owners."""

from contextlib import ExitStack
import copy
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from utils.arrangement_plan import build_plan, project_configuration
from utils.arrangement_runtime import ArrangementRuntime
from utils.arrangement_state import SetupConflict, SetupCoordinator

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "utils/dumb_config.json"
DEFAULTS = json.loads(DEFAULT_PATH.read_text())


class NativeSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dumb-native-setup-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "v1"
        self.source.mkdir()
        self.config = copy.deepcopy(DEFAULTS)
        self.config["data_root"] = str(self.root)
        self.config["puid"] = os.geteuid() or 1000
        self.config["pgid"] = os.getegid() or 1000
        service = self.config["infinidysk"]
        service.update(
            config_dir=str(self.source),
            runtime_generation="1",
            mount_backend="rclone",
            mount_path=str(self.root / "delivery"),
            v2_data_dir=str(self.root / "native-data"),
            v2_runtime_dir=str(self.root / "native-runtime"),
            postgres_enabled=False,
        )
        service["env"]["CONFIG_PATH"] = str(self.source)
        for key in ("DATABASE_PROVIDER", "DATABASE_CONNECTION_STRING"):
            service["env"].pop(key, None)
        template = copy.deepcopy(next(iter(DEFAULTS["rclone"]["instances"].values())))
        template.update(
            enabled=True,
            key_type="infinidysk",
            core_service="infinidysk",
            process_name="Existing custom mount",
            mount_dir=str(self.root),
            mount_name="custom-delivery",
        )
        self.config["rclone"]["instances"] = {"custom-owner": template}
        self.manager = SimpleNamespace(
            config=self.config, default_config_path=DEFAULT_PATH, save_config=Mock()
        )
        self.runtime = ArrangementRuntime(
            self.manager, Mock(), Mock(), logging.getLogger(__name__)
        )

    def payload(self, generation=None, mount=None):
        values = {}
        if generation is not None:
            values["infinidysk::runtime_generation"] = generation
        if mount is not None:
            values["infinidysk::mount_backend"] = mount
        return {
            "core_services": [{"name": "infinidysk", "service_options": {}}],
            "optional_services": [],
            "arrangement": {"values": values},
        }

    def plan(self, payload):
        return build_plan(payload, self.config, DEFAULTS, plan_id="a" * 32)

    def native_current(self, mount="fuse"):
        self.config["infinidysk"].update(runtime_generation="2", mount_backend=mount)
        if mount == "rclone":
            self.config["infinidysk"]["mount_path"] = str(self.root / "custom-delivery")
        else:
            self.config["rclone"]["instances"]["custom-owner"]["enabled"] = False

    def test_default_legacy_plan_preserves_configuration_and_custom_mount_owner(self):
        before = copy.deepcopy(self.config)
        plan = self.plan(self.payload())
        resource = next(
            item for item in plan["services"] if item["service_key"] == "infinidysk"
        )
        self.assertEqual(
            resource["runtime_topology"],
            {
                "runtime_generation": "1",
                "mount_backend": "rclone",
                "mount_path": str(self.root / "custom-delivery"),
                "dependencies": ["rclone"],
            },
        )
        self.assertIn("rclone.custom-owner", [item["id"] for item in plan["services"]])
        self.assertFalse(
            any(item["action"] == "native_transition" for item in plan["actions"])
        )
        self.assertEqual(self.config, before)

    def test_fresh_native_fuse_handoff_blocks_start_before_any_worker_or_enable(self):
        payload = self.payload("2", "fuse")
        coordinator = SetupCoordinator(
            self.root / "setup",
            configuration=lambda: self.config,
            defaults=lambda: DEFAULTS,
            start_services=Mock(),
            inspect_action=Mock(),
            autostart=False,
        )
        self.addCleanup(coordinator.shutdown)
        before = copy.deepcopy(self.config)
        plan = coordinator.plan(payload)
        native = next(
            item for item in plan["services"] if item["service_key"] == "infinidysk"
        )
        self.assertEqual(native["runtime_topology"]["dependencies"], [])
        self.assertFalse(
            any(item["service_key"] == "rclone" for item in plan["services"])
        )
        action = next(
            item for item in plan["actions"] if item["action"] == "native_transition"
        )
        self.assertEqual(action["kind"], "native_transition")
        self.assertEqual(
            action["native_transition"],
            {"runtime_generation": "2", "mode": "fresh", "mount_backend": "fuse"},
        )
        self.assertTrue(
            any(item["critical"] and item["ok"] is False for item in plan["checks"])
        )
        with self.assertRaisesRegex(SetupConflict, "blocking checks"):
            coordinator.start(plan["plan_id"], "native-fixture-request")
        self.assertEqual(coordinator.workers, {})
        coordinator.start_services.assert_not_called()
        self.assertEqual(self.config, before)

    def test_existing_v1_data_uses_major_migration_handoff(self):
        (self.source / "db.sqlite").write_bytes(b"retained original")
        plan = self.plan(self.payload("2", "rclone"))
        action = next(
            item for item in plan["actions"] if item["action"] == "native_transition"
        )
        self.assertEqual(action["native_transition"]["mode"], "migrate")
        self.assertEqual(action["native_transition"]["mount_backend"], "rclone")
        self.assertEqual((self.source / "db.sqlite").read_bytes(), b"retained original")

    def test_raw_service_options_cannot_authorize_native_transition_or_mount_edits(
        self,
    ):
        for options in (
            {"runtime_generation": "2"},
            {"mount_backend": "fuse"},
            {"v2_data_dir": str(self.source)},
            {"env": {"INFINIDYSK_DATABASE_URL": "sqlite:/reserved-example/db.sqlite"}},
        ):
            with self.subTest(options=options):
                payload = self.payload()
                payload["core_services"][0]["service_options"] = {"infinidysk": options}
                with self.assertRaises(ValueError):
                    self.plan(payload)

    def test_existing_native_topology_is_readonly_and_not_downgraded_by_stale_draft(
        self,
    ):
        self.native_current()
        plan = self.plan(self.payload())
        resource = next(
            item for item in plan["services"] if item["service_key"] == "infinidysk"
        )
        self.assertEqual(resource["runtime_topology"]["mount_backend"], "fuse")
        self.assertEqual(
            resource["runtime_topology"]["mount_path"], str(self.root / "delivery")
        )
        fields = {item["key"]: item for item in plan["arrangement_schema"]["fields"]}
        self.assertTrue(fields["infinidysk::runtime_generation"]["readonly"])
        self.assertTrue(fields["infinidysk::mount_backend"]["readonly"])
        self.assertFalse(
            any(item["action"] == "native_transition" for item in plan["actions"])
        )
        for generation, mount in (("1", "rclone"), ("2", "rclone")):
            with self.assertRaises(ValueError):
                self.plan(self.payload(generation, mount))

    def test_existing_native_rclone_reuses_exact_associated_owner(self):
        self.native_current("rclone")
        plan = self.plan(self.payload())
        self.assertEqual(
            [
                item["id"]
                for item in plan["services"]
                if item["service_key"] == "rclone"
            ],
            ["rclone.custom-owner"],
        )
        proposed, _ = project_configuration(self.payload(), self.config, DEFAULTS)
        self.assertEqual(set(proposed["rclone"]["instances"]), {"custom-owner"})

    def test_switching_draft_back_to_legacy_normalizes_mount_to_rclone(self):
        plan = self.plan(self.payload("1", "fuse"))
        resource = next(
            item for item in plan["services"] if item["service_key"] == "infinidysk"
        )
        self.assertEqual(resource["runtime_topology"]["runtime_generation"], "1")
        self.assertEqual(resource["runtime_topology"]["mount_backend"], "rclone")

    def test_runtime_start_independently_blocks_pending_handoff_before_startup(self):
        before = copy.deepcopy(self.config)
        with patch("api.routers.process._run_startup") as startup:
            with self.assertRaisesRegex(ValueError, "guarded InfiniDysk 2"):
                self.runtime.start(self.payload("2", "fuse"))
        startup.assert_not_called()
        self.manager.save_config.assert_not_called()
        self.assertEqual(self.config, before)

    def test_native_generation_never_calls_legacy_nntp_or_arr_adapters(self):
        self.native_current()
        actions = (
            {
                "id": "infinidysk::nntp",
                "source": "infinidysk",
                "service_key": "infinidysk",
                "action": "nntp",
            },
            {
                "id": "sonarr.TV::provider.infinidysk",
                "source": "sonarr.TV",
                "service_key": "sonarr",
                "action": "provider.infinidysk",
                "target": "infinidysk",
            },
        )
        with (
            patch(
                "utils.arrangement_providers.apply_provider",
                side_effect=AssertionError("legacy API"),
            ),
            patch(
                "utils.arrangement_runtime.ArrangementRuntime._provider_link",
                side_effect=AssertionError("legacy Arr API"),
            ),
        ):
            for action in actions:
                outcome = self.runtime.apply(action, self.payload(), "apply")
                self.assertEqual(outcome["automation"], "manual")
                self.assertEqual(outcome["execution"], "waiting_input")
                self.assertEqual(outcome["verification"], "not_checked")

    def test_native_storage_checks_use_active_paths_not_retained_v1_config(self):
        from utils.arrangement_preflight import storage_checks

        self.native_current()
        payload = self.payload()
        plan = self.plan(payload)
        paths = []

        def probe(path, *args):
            paths.append(path)
            return {
                "present": True,
                "accessible": True,
                "filesystem": "ext4",
                "free_bytes": 1024**3,
                "reason": "fixture",
            }

        with patch("utils.arrangement_preflight._probe", side_effect=probe):
            storage_checks(plan, payload, self.config, DEFAULTS)
        self.assertIn(str(self.root / "native-data"), paths)
        self.assertIn(str(self.root / "native-runtime"), paths)
        self.assertNotIn(str(self.source), paths)

    def test_fresh_rclone_handoff_does_not_create_a_mount_before_native_wizard(self):
        self.config["rclone"]["instances"] = copy.deepcopy(
            DEFAULTS["rclone"]["instances"]
        )
        plan = self.plan(self.payload("2", "rclone"))
        native = next(
            item for item in plan["services"] if item["service_key"] == "infinidysk"
        )
        self.assertEqual(native["runtime_topology"]["dependencies"], ["rclone"])
        self.assertFalse(
            any(item["service_key"] == "rclone" for item in plan["services"])
        )
        self.assertTrue(
            any(item["action"] == "native_transition" for item in plan["actions"])
        )

    def test_native_fuse_preflight_requires_device_and_unprivileged_mount_policy(self):
        self.native_current()
        payload = self.payload()
        plan = self.plan(payload)
        original_exists, original_read = Path.exists, Path.read_text

        def exists(path):
            return False if str(path) == "/dev/fuse" else original_exists(path)

        def read(path, *args, **kwargs):
            return (
                "user_allow_other\n"
                if str(path) == "/etc/fuse.conf"
                else original_read(path, *args, **kwargs)
            )

        with (
            patch.object(Path, "exists", exists),
            patch.object(Path, "read_text", read),
            patch("shutil.which", return_value="/usr/bin/fusermount3"),
            patch("utils.arrangement_preflight.storage_checks", return_value=[]),
            patch("utils.arrangement_ports.port_checks", return_value=[]),
        ):
            checks = self.runtime.preflight(plan, payload)
        by_key = {item["key"]: item for item in checks}
        self.assertFalse(by_key["fuse_device"]["ok"])
        self.assertTrue(by_key["fuse_device"]["critical"])
        self.assertTrue(by_key["native_fuse_policy"]["ok"])

    def test_native_local_data_preflight_refuses_network_storage(self):
        from utils.backup_storage import BackupStorageError

        self.native_current()
        payload = self.payload()
        plan = self.plan(payload)
        with (
            patch(
                "utils.backup_storage.require_local_path",
                side_effect=BackupStorageError("network fixture"),
            ),
            patch("utils.arrangement_preflight.storage_checks", return_value=[]),
            patch("utils.arrangement_ports.port_checks", return_value=[]),
        ):
            checks = self.runtime.preflight(plan, payload)
        native = [item for item in checks if item["key"].startswith("native_local_")]
        self.assertEqual(len(native), 2)
        self.assertTrue(
            all(item["critical"] and item["ok"] is False for item in native)
        )

    def test_legacy_source_drafts_cannot_reconfigure_an_existing_native_runtime(self):
        self.native_current()
        for options in (
            {"branch_enabled": True},
            {"commit_sha": "a" * 40},
            {"repo_owner": "other-owner"},
            {"command": ["legacy-app"]},
            {"config_dir": str(self.root / "different-retained-source")},
        ):
            with self.subTest(options=options):
                payload = self.payload()
                payload["core_services"][0]["service_options"] = {"infinidysk": options}
                with self.assertRaisesRegex(ValueError, "pinned native"):
                    self.plan(payload)

    def test_low_level_legacy_provider_entrypoints_refuse_native_before_http_or_db(
        self,
    ):
        from utils.arrangement_providers import _client, UnsupportedProviderVersion
        from utils.arrangement_external_provider import (
            external_arr_provider_credentials_and_category,
        )

        self.native_current()
        factory = Mock(side_effect=AssertionError("legacy HTTP"))
        with self.assertRaises(UnsupportedProviderVersion):
            _client("infinidysk", self.config["infinidysk"], factory)
        result = external_arr_provider_credentials_and_category(
            self.config,
            {},
            "infinidysk",
            {},
            "fixture",
            "apply",
            client_factory=factory,
        )
        self.assertEqual(result["execution"], "waiting_input")
        factory.assert_not_called()

    def test_native_workflow_probe_is_not_sent_to_unqualified_legacy_adapter(self):
        self.native_current()
        payload = self.payload()
        payload["arrangement"]["values"]["sonarr.TV::workflow_probe"] = {
            "provider": "infinidysk"
        }
        action = {
            "id": "sonarr.TV::workflow_test",
            "source": "sonarr.TV",
            "service_key": "sonarr",
            "action": "workflow_test",
        }
        with patch(
            "utils.arrangement_workflow.workflow_action",
            side_effect=AssertionError("legacy workflow"),
        ):
            outcome = self.runtime.apply(action, payload, "apply")
        self.assertEqual(outcome["automation"], "manual")
        self.assertEqual(outcome["verification"], "not_checked")


def _native_admission_proof(case):
    # A fresh interpreter imports the real FastAPI graph. Legacy unittest
    # modules intentionally keep minimal global API doubles during discovery.
    class AdmissionCase(unittest.TestCase):
        def guard_patches(self):
            stack = ExitStack()
            for name in (
                "infinidysk_namespace_recovery_pending",
                "infinidysk_postgres_recovery_pending",
                "infinidysk_v2_recovery_pending",
                "infinidysk_namespace_migration_active",
                "infinidysk_postgres_migration_active",
                "infinidysk_v2_jobs_active",
            ):
                stack.enter_context(
                    patch(
                        "utils.infinidysk_migration_admission." + name,
                        return_value=False,
                    )
                )
            manager = stack.enter_context(
                patch("utils.arr_postgres_migration.POSTGRES_MIGRATION_MANAGER")
            )
            manager.has_setup_conflicting_job.return_value = False
            manager.has_namespace_conflicting_job.return_value = False
            return stack

        def test_setup_reservation_blocks_native_start_recovery_and_retirement_callbacks(
            self,
        ):
            from api.routers.setup import _mutation_guard
            from api.routers import infinidysk_v2 as router
            from fastapi import HTTPException

            with self.guard_patches(), patch.object(router, "_manager") as selected:
                manager = selected.return_value
                with _mutation_guard():
                    for kind, operation in (
                        ("major", "start"),
                        ("major", "rehearse"),
                        ("major", "rollback"),
                        ("major", "cleanup"),
                        ("major", "cleanup_preview"),
                        ("postgres", "start"),
                        ("postgres", "cutover"),
                        ("postgres", "rollback"),
                        ("postgres", "cancel"),
                    ):
                        with self.subTest(kind=kind, operation=operation):
                            with self.assertRaises(HTTPException) as caught:
                                router._invoke(
                                    kind, operation, {}, Mock(), Mock(), mutation=True
                                )
                            self.assertEqual(caught.exception.status_code, 409)
                            getattr(manager, operation).assert_not_called()

        def test_native_migration_provider_or_cleanup_reservation_blocks_setup(self):
            from api.routers.setup import _mutation_guard

            with (
                self.guard_patches(),
                patch(
                    "utils.infinidysk_migration_admission.infinidysk_v2_jobs_active",
                    return_value=True,
                ),
            ):
                with self.assertRaises(SetupConflict):
                    with _mutation_guard():
                        self.fail("native owner was bypassed")

        def test_native_recovery_blocks_setup_before_external_reservation(self):
            from api.routers.setup import _mutation_guard

            with (
                self.guard_patches(),
                patch(
                    "utils.infinidysk_migration_admission.infinidysk_v2_recovery_pending",
                    return_value=True,
                ),
                patch(
                    "utils.infinidysk_migration_admission.reserve_infinidysk_external_mutation"
                ) as reserve,
            ):
                with self.assertRaises(SetupConflict):
                    with _mutation_guard():
                        self.fail("native recovery was bypassed")
                reserve.assert_not_called()

    suite = unittest.TestSuite([AdmissionCase(case)])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)


class NativeSetupAdmissionTests(unittest.TestCase):
    def real_api_case(self, case):
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(root)
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "from tests.test_setup_infinidysk_v2 import _native_admission_proof; "
                + "_native_admission_proof("
                + repr(case)
                + ")",
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 1 test", result.stderr)
        self.assertIn("OK", result.stderr)

    def test_setup_reservation_blocks_native_start_recovery_and_retirement_callbacks(
        self,
    ):
        self.real_api_case(
            "test_setup_reservation_blocks_native_start_recovery_and_retirement_callbacks"
        )

    def test_native_migration_provider_or_cleanup_reservation_blocks_setup(self):
        self.real_api_case(
            "test_native_migration_provider_or_cleanup_reservation_blocks_setup"
        )

    def test_native_recovery_blocks_setup_before_external_reservation(self):
        self.real_api_case(
            "test_native_recovery_blocks_setup_before_external_reservation"
        )
