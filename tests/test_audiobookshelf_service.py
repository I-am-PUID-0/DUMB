import asyncio
import copy
import importlib
import sys
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jsonschema import ValidationError, validate

from utils import setup
from utils.auto_update import Update
from utils.audiobookshelf_settings import (
    AudiobookshelfConfigurationError,
    audiobookshelf_launch_config,
    audiobookshelf_paths,
)
from utils.service_health import ServiceHealthMonitor
from utils.versions import Versions

ROOT = Path(__file__).resolve().parents[1]


class AudiobookshelfServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="dumb-audiobookshelf-service-"
        )
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = copy.deepcopy(
            json.loads((ROOT / "utils/dumb_config.json").read_text())["audiobookshelf"]
        )
        self.config.update(
            enabled=True,
            runtime_dir=str(self.root / "runtime"),
            config_dir=str(self.root / "config"),
            metadata_dir=str(self.root / "metadata"),
        )
        self.manager = SimpleNamespace(
            get=lambda key: {
                "audiobookshelf": self.config,
                "puid": os.geteuid(),
                "pgid": os.getegid(),
            }.get(key),
            save_config=Mock(),
        )

    def _setup_context(self):
        return (
            patch.object(setup, "CONFIG_MANAGER", self.manager),
            patch.object(setup, "validate_audiobookshelf_node_runtime"),
            patch("utils.user_management._filesystem_type", return_value="ext4"),
            patch(
                "utils.audiobookshelf_settings.shutil.which",
                side_effect=lambda name: "/usr/bin/" + name,
            ),
        )

    def _updater(self):
        updater = object.__new__(Update)
        updater.logger = Mock()
        updater.process_handler = Mock()
        updater.process_handler.process_names = {"Audiobookshelf"}
        updater.process_handler.setup_tracker = {"Audiobookshelf"}
        updater.process_handler.setup_tracker_lock = threading.Lock()
        updater.stop_process = Mock()
        updater.start_process = Mock(return_value=(Mock(), None))
        return updater

    @staticmethod
    def _target(installed=False):
        return {
            "selector": "latest",
            "installed": installed,
            "current_version": "v2.37.0",
            "available_version": "v2.37.1",
            "release_tag": "v2.37.1",
            "commit_sha": "a" * 40,
        }

    def test_defaults_validate_and_service_is_optional(self):
        defaults = json.loads((ROOT / "utils/dumb_config.json").read_text())
        schema = json.loads((ROOT / "utils/dumb_config_schema.json").read_text())
        validate(defaults, schema)
        self.assertFalse(defaults["audiobookshelf"]["enabled"])
        self.assertIn("audiobookshelf", setup.SERVICE_MANAGED_SOURCE_KEYS)
        self.assertIn("audiobookshelf", setup.COMMIT_PIN_SERVICE_KEYS)
        validate(self.config, schema["properties"]["audiobookshelf"])

    def test_optional_api_advertises_configurable_storage_without_core_dependencies(
        self,
    ):
        fixture = next(
            (
                sys.modules[name]
                for name in (
                    "test_process_update_notices",
                    "tests.test_process_update_notices",
                )
                if name in sys.modules
            ),
            None,
        )
        if fixture is None:
            fixture = importlib.import_module("tests.test_process_update_notices")
        router = fixture.process_router
        manager = SimpleNamespace(
            default_config_path=str(ROOT / "utils/dumb_config.json"), config={}
        )
        with patch.object(router, "CONFIG_MANAGER", manager):
            response = asyncio.run(
                router.get_optional_services(
                    logger=Mock(), core_service=None, optional_services=[]
                )
            )
        service = next(
            item
            for item in response["optional_services"]
            if item["key"] == "audiobookshelf"
        )
        self.assertEqual(service["service_options"]["port"], 13378)
        self.assertEqual(
            service["service_options"]["config_dir"], "/audiobookshelf/config"
        )
        self.assertEqual(
            service["service_options"]["metadata_dir"], "/audiobookshelf/metadata"
        )
        self.assertIn("local", service["service_option_descriptions"]["config_dir"])
        self.assertFalse(service["service_options"]["branch_enabled"])
        self.assertEqual(service["service_options"]["branch"], "master")
        self.assertEqual(service["service_options"]["commit_sha"], "")
        self.assertIn("official", service["service_option_descriptions"]["commit_sha"])
        self.assertNotIn("audiobookshelf", router._POSTGRES_REQUIRED_OPTIONALS)
        self.assertIn("audiobookshelf", router.OPTIONAL_POST_CORE)
        self.assertFalse(service["supports_instances"])

    def test_launch_environment_preserves_custom_values_and_owns_binding_paths(self):
        self.config["env"].update(
            PORT="1",
            CONFIG_PATH="/wrong",
            METADATA_PATH="/wrong",
            ROUTER_BASE_PATH="/wrong",
            ALLOW_IFRAME="0",
            CUSTOM_OPTION="example",
        )
        before = copy.deepcopy(self.config)
        with patch(
            "utils.audiobookshelf_settings.shutil.which",
            side_effect=lambda name: "/usr/bin/" + name,
        ):
            launch = audiobookshelf_launch_config(self.config)
        self.assertEqual(launch["env"]["PORT"], "13378")
        self.assertEqual(launch["env"]["CONFIG_PATH"], self.config["config_dir"])
        self.assertEqual(launch["env"]["METADATA_PATH"], self.config["metadata_dir"])
        self.assertEqual(launch["env"]["ROUTER_BASE_PATH"], "/audiobookshelf")
        self.assertEqual(launch["env"]["ALLOW_IFRAME"], "1")
        self.assertEqual(launch["env"]["CUSTOM_OPTION"], "example")
        self.assertEqual(
            launch["command"],
            ["/usr/bin/node", str(self.root / "runtime/dist-server/index.js")],
        )
        self.assertEqual(self.config, before)
        self.assertFalse((self.root / "config").exists())

    def test_runtime_data_overlap_and_unsafe_roots_are_rejected(self):
        cases = (
            {"runtime_dir": str(self.root / "config/runtime")},
            {"config_dir": str(self.root / "runtime/config")},
            {"metadata_dir": str(self.root / "runtime")},
            {"metadata_dir": str(self.root / "config")},
            {"config_dir": "/"},
            {"config_dir": "relative"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(AudiobookshelfConfigurationError):
                    audiobookshelf_paths({**self.config, **changes})

    def test_missing_image_binary_reports_actionable_error(self):
        with patch("utils.audiobookshelf_settings.shutil.which", return_value=None):
            with self.assertRaisesRegex(
                AudiobookshelfConfigurationError, "update or rebuild"
            ):
                audiobookshelf_launch_config(self.config)

    def test_configure_prepares_persistent_dirs_without_chowning_runtime(self):
        patches = self._setup_context()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.object(setup, "audiobookshelf_runtime_ready", return_value=True),
            patch.object(
                setup, "audiobookshelf_runtime_matches_selection", return_value=True
            ),
            patch.object(setup, "install_audiobookshelf_runtime") as install,
            patch.object(setup, "_chown_recursive_if_needed") as chown,
        ):
            success, error = setup.setup_audiobookshelf(Mock(), configure_only=True)
        self.assertTrue(success, error)
        install.assert_not_called()
        self.assertTrue((self.root / "config").is_dir())
        self.assertTrue((self.root / "metadata").is_dir())
        self.assertEqual(
            [call.args[0] for call in chown.call_args_list],
            [str(self.root / "config"), str(self.root / "metadata")],
        )
        self.assertEqual(self.config["env"]["CONFIG_PATH"], self.config["config_dir"])
        self.manager.save_config.assert_called_once_with("Audiobookshelf")

    def test_missing_runtime_does_not_configure_or_create_a_library(self):
        patches = self._setup_context()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.object(setup, "audiobookshelf_runtime_ready", return_value=False),
            patch.object(setup, "install_audiobookshelf_runtime") as install,
        ):
            success, error = setup.setup_audiobookshelf(Mock(), configure_only=True)
        self.assertFalse(success)
        self.assertIn("install phase", error)
        install.assert_not_called()
        self.manager.save_config.assert_not_called()
        self.assertFalse((self.root / "config").exists())

    def test_remote_sqlite_is_rejected_before_install_or_mutation(self):
        for filesystem in ("nfs", "nfs4", "cifs", "smb3"):
            with self.subTest(filesystem=filesystem):
                patches = self._setup_context()
                with (
                    patches[0],
                    patches[1],
                    patches[3],
                    patch(
                        "utils.user_management._filesystem_type",
                        return_value=filesystem,
                    ),
                    patch.object(setup, "install_audiobookshelf_runtime") as install,
                ):
                    success, error = setup.setup_audiobookshelf(
                        Mock(), install_only=True
                    )
                self.assertFalse(success)
                self.assertIn("local filesystem", error)
                install.assert_not_called()
                self.assertFalse((self.root / "config").exists())

    def test_failed_configuration_save_restores_memory(self):
        before = copy.deepcopy(self.config)
        self.manager.save_config.side_effect = OSError("fixture save failure")
        patches = self._setup_context()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.object(setup, "audiobookshelf_runtime_ready", return_value=True),
            patch.object(
                setup, "audiobookshelf_runtime_matches_selection", return_value=True
            ),
        ):
            success, error = setup.setup_audiobookshelf(Mock(), configure_only=True)
        self.assertFalse(success)
        self.assertIn("fixture save failure", error)
        self.assertEqual(self.config, before)

    def test_latest_startup_can_reuse_offline_runtime(self):
        updater = self._updater()
        with (
            patch("utils.auto_update.audiobookshelf_runtime_ready", return_value=True),
            patch(
                "utils.auto_update.audiobookshelf_runtime_matches_selection",
                return_value=True,
            ),
            patch("utils.auto_update.audiobookshelf_target_status") as online_check,
        ):
            needs_install = updater._should_run_install_phase_for_preinstalled(
                "Audiobookshelf", "audiobookshelf", None, self.config
            )
        self.assertFalse(needs_install)
        online_check.assert_not_called()

    def test_update_build_failure_keeps_running_process_untouched(self):
        from utils.audiobookshelf_installer import AudiobookshelfInstallError

        updater = self._updater()
        with (
            patch("utils.auto_update._audiobookshelf_launch_configuration"),
            patch(
                "utils.auto_update.audiobookshelf_target_status",
                return_value=self._target(),
            ),
            patch(
                "utils.auto_update.install_audiobookshelf_runtime",
                side_effect=AudiobookshelfInstallError("candidate fixture failed"),
            ),
            patch("utils.auto_update.configure_project") as configure,
        ):
            success, error = updater.update_check(
                "Audiobookshelf", self.config, "audiobookshelf", None
            )
        self.assertFalse(success)
        self.assertIn("candidate fixture failed", error)
        updater.stop_process.assert_not_called()
        updater.start_process.assert_not_called()
        configure.assert_not_called()
        self.assertEqual(updater.process_handler.setup_tracker, {"Audiobookshelf"})

    def test_configured_release_validates_candidate_before_stopping(self):
        updater = self._updater()
        self.config.update(release_version_enabled=True, release_version="v2.37.1")
        events = []
        updater.stop_process.side_effect = lambda name: events.append("stopped")
        updater.start_process.side_effect = lambda *args: (
            events.append("started") or Mock(),
            None,
        )

        def install(config, *, before_activate, force):
            self.assertTrue(force)
            self.assertFalse(updater.stop_process.called)
            events.append("validated")
            before_activate()
            return {"version": "v2.37.1"}

        def configure(*args):
            events.append("configured")
            return True, None

        with (
            patch("utils.auto_update._audiobookshelf_launch_configuration"),
            patch(
                "utils.auto_update.audiobookshelf_target_status",
                return_value=self._target(),
            ),
            patch(
                "utils.auto_update.install_audiobookshelf_runtime", side_effect=install
            ),
            patch("utils.auto_update.configure_project", side_effect=configure),
            patch("utils.auto_update.setup_release_version") as generic,
        ):
            success, error = updater._install_configured_target(
                "Audiobookshelf", self.config, "audiobookshelf", None, "release"
            )
        self.assertTrue(success, error)
        self.assertEqual(events, ["validated", "stopped", "configured", "started"])
        generic.assert_not_called()
        self.assertEqual(updater.process_handler.setup_tracker, set())

    def test_fixed_release_check_reports_configured_target_without_advancing(self):
        updater = self._updater()
        self.config.update(release_version_enabled=True, release_version="v2.37.1")
        with patch(
            "utils.auto_update.audiobookshelf_target_status",
            return_value=self._target(),
        ):
            payload = updater._manual_check_audiobookshelf(
                "Audiobookshelf", self.config, "release", 100, True, 24, "04:00", 200
            )
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["available_version"], "v2.37.1")
        self.assertEqual(payload["configured_target_kind"], "release")
        self.assertFalse(payload["configured_target_installed"])
        self.assertIsNone(payload["next_check_at"])

    def test_source_schema_accepts_branch_and_commit_and_rejects_partial_pins(self):
        schema = json.loads((ROOT / "utils/dumb_config_schema.json").read_text())
        service_schema = schema["properties"]["audiobookshelf"]
        validate(
            {**self.config, "branch_enabled": True, "branch": "feature/example"},
            service_schema,
        )
        validate({**self.config, "commit_sha": "A" * 40}, service_schema)
        for value in ("abc123", "refs/heads/master", "g" * 40):
            with self.subTest(commit_sha=value):
                with self.assertRaises(ValidationError):
                    validate({**self.config, "commit_sha": value}, service_schema)

    def test_source_check_reports_configured_kind_and_revision(self):
        updater = self._updater()
        for kind, changes in (
            ("branch", {"branch_enabled": True, "branch": "master"}),
            ("commit", {"commit_sha": "a" * 40}),
        ):
            for installed in (False, True):
                with self.subTest(kind=kind, installed=installed):
                    self.config.update(branch_enabled=False, commit_sha="")
                    self.config.update(changes)
                    target = {
                        **self._target(installed),
                        "selector": f"{kind}:"
                        + ("master" if kind == "branch" else "a" * 40),
                        "current_version": (
                            ("master" if kind == "branch" else "commit")
                            + "-"
                            + "b" * 12
                        ),
                        "available_version": (
                            ("master" if kind == "branch" else "commit")
                            + "-"
                            + "a" * 12
                        ),
                        "current_commit_sha": "b" * 40,
                    }
                    self.assertEqual(
                        updater._get_update_block_reason(self.config, "audiobookshelf"),
                        kind,
                    )
                    with patch(
                        "utils.auto_update.audiobookshelf_target_status",
                        return_value=target,
                    ):
                        payload = updater._manual_update_check_internal(
                            "Audiobookshelf", self.config, "audiobookshelf", None
                        )
                    self.assertEqual(payload["configured_target_kind"], kind)
                    self.assertEqual(payload["configured_target_installed"], installed)
                    self.assertEqual(
                        payload["status"], "no_update" if installed else "blocked"
                    )
                    self.assertEqual(payload["current_commit_sha"], "b" * 40)
                    self.assertEqual(payload["available_commit_sha"], "a" * 40)
                    self.assertEqual(
                        payload["available_version"], target["available_version"]
                    )
                    self.assertFalse(payload["auto_update_enabled"])
                    self.assertIsNone(payload["next_check_at"])

    def test_explicit_branch_and_commit_install_uses_validated_candidate(self):
        for kind, changes in (
            ("branch", {"branch_enabled": True, "branch": "master"}),
            ("commit", {"commit_sha": "a" * 40}),
        ):
            with self.subTest(kind=kind):
                self.config.update(branch_enabled=False, commit_sha="")
                self.config.update(changes)
                updater = self._updater()
                events = []
                updater.stop_process.side_effect = lambda name: events.append("stopped")
                display = ("master" if kind == "branch" else "commit") + "-" + "a" * 12

                def install(config, *, before_activate, force):
                    self.assertEqual(config, self.config)
                    self.assertTrue(force)
                    self.assertFalse(updater.stop_process.called)
                    events.append("validated")
                    before_activate()
                    return {"version": "2.37.1", "display_version": display}

                with (
                    patch("utils.auto_update._audiobookshelf_launch_configuration"),
                    patch(
                        "utils.auto_update.audiobookshelf_target_status",
                        return_value=self._target(),
                    ),
                    patch(
                        "utils.auto_update.install_audiobookshelf_runtime",
                        side_effect=install,
                    ),
                    patch(
                        "utils.auto_update.configure_project",
                        return_value=(True, None),
                    ) as configure,
                    patch("utils.auto_update.setup_branch_version") as generic,
                ):
                    success, message = updater._install_configured_target(
                        "Audiobookshelf", self.config, "audiobookshelf", None, kind
                    )
                self.assertTrue(success, message)
                self.assertIn(display, message)
                self.assertEqual(events, ["validated", "stopped"])
                configure.assert_called_once()
                updater.start_process.assert_called_once()
                generic.assert_not_called()
                self.assertEqual(updater.process_handler.setup_tracker, set())

    def test_scheduled_checks_do_not_advance_source_pins(self):
        for changes in (
            {"branch_enabled": True, "branch": "master"},
            {"commit_sha": "a" * 40},
        ):
            with self.subTest(changes=changes):
                self.config.update(branch_enabled=False, commit_sha="")
                self.config.update(changes)
                updater = self._updater()
                with (
                    patch(
                        "utils.auto_update.audiobookshelf_target_status",
                        return_value=self._target(),
                    ),
                    patch(
                        "utils.auto_update.install_audiobookshelf_runtime"
                    ) as install,
                ):
                    payload = updater._scheduled_update_check_admitted(
                        "Audiobookshelf", self.config, "audiobookshelf", None
                    )
                self.assertEqual(payload["status"], "blocked")
                install.assert_not_called()
                updater.stop_process.assert_not_called()

    def test_branch_startup_reuses_verified_runtime_without_online_lookup(self):
        self.config.update(branch_enabled=True, branch="master")
        updater = self._updater()
        with (
            patch("utils.auto_update.audiobookshelf_runtime_ready", return_value=True),
            patch(
                "utils.auto_update.audiobookshelf_runtime_matches_selection",
                return_value=True,
            ) as matches,
            patch("utils.auto_update.audiobookshelf_target_status") as online,
        ):
            self.assertFalse(
                updater._should_run_install_phase_for_preinstalled(
                    "Audiobookshelf", "audiobookshelf", None, self.config
                )
            )
        matches.assert_called_once_with(self.config["runtime_dir"], "branch:master")
        online.assert_not_called()

    def test_runtime_snapshots_exclude_live_database(self):
        updater = self._updater()
        for field in ("runtime_dir", "config_dir", "metadata_dir"):
            Path(self.config[field]).mkdir()
        database = Path(self.config["config_dir"], "absdatabase.sqlite")
        database.write_bytes(b"preserved fixture")
        with patch("utils.auto_update.audiobookshelf_runtime_ready", return_value=True):
            self.assertEqual(
                updater._snapshot_target("audiobookshelf", self.config),
                str(self.root / "runtime"),
            )
        self.assertEqual(database.read_bytes(), b"preserved fixture")

    def test_invalid_or_unmanaged_runtime_never_snapshots_data(self):
        updater = self._updater()
        Path(self.config["config_dir"]).mkdir()
        for runtime in ("/", self.config["config_dir"], str(self.root)):
            with self.subTest(runtime=runtime):
                with patch(
                    "utils.auto_update.audiobookshelf_runtime_ready", return_value=True
                ):
                    self.assertIsNone(
                        updater._snapshot_target(
                            "audiobookshelf", {**self.config, "runtime_dir": runtime}
                        )
                    )
        Path(self.config["runtime_dir"]).mkdir()
        self.assertIsNone(updater._snapshot_target("audiobookshelf", self.config))

    def test_versions_reads_custom_runtime_marker(self):
        runtime = Path(self.config["runtime_dir"])
        runtime.mkdir()
        (runtime / "version.txt").write_text("v2.37.1")
        manager = SimpleNamespace(get_instance=lambda *args: self.config)
        with patch.dict(
            Versions.version_check.__globals__, {"CONFIG_MANAGER": manager}
        ):
            version, error = Versions().version_check(
                "Audiobookshelf", None, "audiobookshelf"
            )
        self.assertIsNone(error)
        self.assertEqual(version, "v2.37.1")

    def test_native_healthcheck_uses_selected_service_port(self):
        class Raw:
            def read(self, amount, decode_content=True):
                return b"OK"

        response = Mock(
            status_code=200, raw=Raw(), headers={"content-type": "text/plain"}
        )
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch(
            "utils.service_health.requests.request", return_value=response
        ) as request:
            result = ServiceHealthMonitor(cache_ttl_seconds=0).check(
                "audiobookshelf", "Audiobookshelf", {"port": 13379}
            )
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(
            request.call_args.args[:2], ("GET", "http://127.0.0.1:13379/healthcheck")
        )


if __name__ == "__main__":
    unittest.main()
