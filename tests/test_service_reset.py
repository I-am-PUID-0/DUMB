import copy
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from utils.service_reset import (
    ServiceResetError,
    build_service_reset_preview,
    execute_service_reset,
)


class FakeConfigManager:
    def __init__(self, root: Path, defaults: dict, config: dict):
        self.default_config_path = str(root / "defaults.json")
        self.file_path = str(root / "dumb_config.json")
        self.config = copy.deepcopy(config)
        Path(self.default_config_path).write_text(json.dumps(defaults))
        self.save_config()

    def find_key_for_process(self, process_name):
        for key, section in self.config.items():
            if not isinstance(section, dict):
                continue
            if section.get("process_name") == process_name:
                return key, None
            instances = section.get("instances")
            if isinstance(instances, dict):
                for instance_name, instance in instances.items():
                    if instance.get("process_name") == process_name:
                        return key, instance_name
        return None, None

    def save_config(self):
        Path(self.file_path).write_text(json.dumps(self.config))


class FakeProcessHandler:
    def __init__(self):
        self.stopped = []

    def stop_process(self, process_name):
        self.stopped.append(process_name)


class RunningProcess:
    pid = 4242

    @staticmethod
    def poll():
        return None


class FailedStopProcessHandler(FakeProcessHandler):
    def __init__(self, process_name):
        super().__init__()
        self.process_names = {process_name: RunningProcess()}

    @staticmethod
    def _prefixed_name(process_name):
        return process_name

    @staticmethod
    def _process_group_alive(process_group):
        return True


class ServiceResetTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.data_root = self.root / "data"
        self.data_root.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def _arr_defaults(self):
        config_dir = self.root / "sonarr" / "default"
        return {
            "data_root": str(self.data_root),
            "sonarr": {
                "instances": {
                    "Default": {
                        "enabled": False,
                        "process_name": "Sonarr",
                        "port": 8989,
                        "config_dir": str(config_dir),
                        "config_file": str(config_dir / "config.xml"),
                        "log_file": str(config_dir / "logs" / "sonarr.txt"),
                        "command": ["Sonarr", "-data", str(config_dir)],
                        "api_key": "",
                        "env": {},
                    }
                }
            },
        }

    def test_remove_custom_instance_deletes_only_instance_paths(self):
        defaults = self._arr_defaults()
        movies_dir = self.root / "sonarr" / "movies"
        movies_data = self.data_root / "sonarr" / "movies"
        other_dir = self.root / "sonarr" / "other"
        movies_dir.mkdir(parents=True)
        movies_data.mkdir(parents=True)
        other_dir.mkdir(parents=True)
        (movies_dir / "runtime.txt").write_text("remove")
        (movies_data / "library.db").write_text("remove")
        (other_dir / "keep.txt").write_text("keep")
        config = copy.deepcopy(defaults)
        config["sonarr"]["instances"] = {
            "Movies": {
                **copy.deepcopy(defaults["sonarr"]["instances"]["Default"]),
                "enabled": True,
                "process_name": "Sonarr Movies",
                "port": 8990,
                "config_dir": str(movies_dir),
                "config_file": str(movies_dir / "config.xml"),
                "log_file": str(movies_dir / "logs" / "sonarr.txt"),
                "api_key": "secret",
            },
            "Other": {
                **copy.deepcopy(defaults["sonarr"]["instances"]["Default"]),
                "process_name": "Sonarr Other",
                "config_dir": str(other_dir),
                "config_file": str(other_dir / "config.xml"),
                "log_file": str(other_dir / "logs" / "sonarr.txt"),
            },
        }
        manager = FakeConfigManager(self.root, defaults, config)
        handler = FakeProcessHandler()

        preview = build_service_reset_preview(manager, "Sonarr Movies", "remove")
        self.assertEqual(preview["config_action"], "remove_instance")
        planned = {target["resolved_path"] for target in preview["file_targets"]}
        self.assertIn(str(movies_dir), planned)
        self.assertIn(str(movies_data), planned)
        self.assertNotIn(str(other_dir), planned)

        result = execute_service_reset(
            manager, handler, "Sonarr Movies", "remove", "Sonarr Movies"
        )

        self.assertEqual(handler.stopped, ["Sonarr Movies"])
        self.assertNotIn("Movies", manager.config["sonarr"]["instances"])
        self.assertTrue((other_dir / "keep.txt").exists())
        self.assertEqual(list(movies_dir.iterdir()), [])
        self.assertEqual(list(movies_data.iterdir()), [])
        backup = Path(result["config_backup_path"])
        self.assertTrue(backup.exists())
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_reset_custom_instance_keeps_files_and_identity_but_clears_secret(self):
        defaults = self._arr_defaults()
        movies_dir = self.root / "sonarr" / "movies"
        movies_dir.mkdir(parents=True)
        retained = movies_dir / "library.db"
        retained.write_text("keep")
        current = copy.deepcopy(defaults["sonarr"]["instances"]["Default"])
        current.update(
            {
                "enabled": True,
                "process_name": "Sonarr Movies",
                "port": 8990,
                "config_dir": str(movies_dir),
                "config_file": str(movies_dir / "config.xml"),
                "log_file": str(movies_dir / "logs" / "sonarr.txt"),
                "api_key": "secret",
            }
        )
        config = copy.deepcopy(defaults)
        config["sonarr"]["instances"] = {"Movies": current}
        manager = FakeConfigManager(self.root, defaults, config)

        execute_service_reset(
            manager,
            FakeProcessHandler(),
            "Sonarr Movies",
            "reset",
            "Sonarr Movies",
        )

        reset = manager.config["sonarr"]["instances"]["Movies"]
        self.assertFalse(reset["enabled"])
        self.assertEqual(reset["process_name"], "Sonarr Movies")
        self.assertEqual(reset["port"], 8990)
        self.assertEqual(reset["config_dir"], str(movies_dir))
        self.assertEqual(reset["api_key"], "")
        self.assertTrue(retained.exists())

    def test_remove_last_custom_instance_immediately_restores_disabled_default(self):
        defaults = self._arr_defaults()
        custom_dir = self.root / "sonarr" / "postgresql"
        custom_dir.mkdir(parents=True)
        current = copy.deepcopy(defaults["sonarr"]["instances"]["Default"])
        current.update(
            {
                "enabled": True,
                "process_name": "Sonarr PostgreSQL",
                "config_dir": str(custom_dir),
                "config_file": str(custom_dir / "config.xml"),
                "log_file": str(custom_dir / "logs" / "sonarr.txt"),
            }
        )
        config = copy.deepcopy(defaults)
        config["sonarr"]["instances"] = {"PostgreSQL": current}
        manager = FakeConfigManager(self.root, defaults, config)

        preview = build_service_reset_preview(manager, "Sonarr PostgreSQL", "remove")
        self.assertEqual(preview["default_instance_after_removal"], "Default")

        execute_service_reset(
            manager,
            FakeProcessHandler(),
            "Sonarr PostgreSQL",
            "remove",
            "Sonarr PostgreSQL",
        )

        expected = copy.deepcopy(defaults["sonarr"]["instances"]["Default"])
        expected["enabled"] = False
        self.assertEqual(manager.config["sonarr"]["instances"], {"Default": expected})
        persisted = json.loads(Path(manager.file_path).read_text())
        self.assertEqual(persisted["sonarr"]["instances"], {"Default": expected})

    def test_shared_directory_is_retained(self):
        shared = self.root / "cli_debrid"
        data_shared = self.data_root / "cli_debrid"
        shared.mkdir()
        data_shared.mkdir()
        defaults = {
            "data_root": str(self.data_root),
            "cli_debrid": {
                "enabled": False,
                "process_name": "CLI Debrid",
                "config_dir": str(shared),
                "config_file": str(shared / "data" / "config.json"),
                "log_file": str(shared / "data" / "debrid.log"),
            },
            "cli_battery": {
                "enabled": False,
                "process_name": "CLI Battery",
                "config_dir": str(shared),
                "config_file": str(shared / "data" / "battery.json"),
                "log_file": str(shared / "data" / "battery.log"),
            },
        }
        config = copy.deepcopy(defaults)
        config["cli_debrid"]["enabled"] = True
        manager = FakeConfigManager(self.root, defaults, config)

        preview = build_service_reset_preview(manager, "CLI Debrid", "remove")

        directory_paths = {
            target["path"]
            for target in preview["file_targets"]
            if target["kind"] == "directory"
        }
        self.assertNotIn(str(shared), directory_paths)
        self.assertNotIn(str(data_shared), directory_paths)
        self.assertTrue(
            any("Retained shared path" in item for item in preview["warnings"])
        )

    def test_custom_directory_outside_managed_root_is_not_deleted(self):
        defaults = self._arr_defaults()
        unsafe = self.root / "unrelated"
        unsafe.mkdir()
        config = copy.deepcopy(defaults)
        config["sonarr"]["instances"]["Default"]["enabled"] = True
        config["sonarr"]["instances"]["Default"]["config_dir"] = str(unsafe)
        manager = FakeConfigManager(self.root, defaults, config)

        preview = build_service_reset_preview(manager, "Sonarr", "remove")

        self.assertNotIn(
            str(unsafe), {target["path"] for target in preview["file_targets"]}
        )
        self.assertTrue(any("outside" in item for item in preview["warnings"]))

    def _audiobookshelf_defaults(self, *, recognized_runtime=True):
        persistent = self.data_root / "audiobookshelf"
        config_dir = persistent / "config"
        metadata_dir = persistent / "metadata"
        runtime_dir = self.root / "opt" / "audiobookshelf"
        for directory in (config_dir, metadata_dir, runtime_dir):
            directory.mkdir(parents=True)
        namespace = self.root / "audiobookshelf"
        namespace.symlink_to(persistent, target_is_directory=True)
        (config_dir / "absdatabase.sqlite").write_text("configuration")
        (metadata_dir / "cover.jpg").write_text("metadata")
        (persistent / "unrelated.txt").write_text("keep")
        (runtime_dir / "incomplete-build.txt").write_text("runtime")
        if recognized_runtime:
            marker = runtime_dir / ".dumb-audiobookshelf.json"
            marker.write_text(
                json.dumps(
                    {
                        "version": "2.37.1",
                        "release_tag": "v2.37.1",
                        "install_selector": "latest",
                        "commit_sha": "a" * 40,
                        "node_major": 24,
                    }
                )
            )
            marker.chmod(0o644)
        runtime_dir.chmod(0o755)
        log_file = self.root / "log" / "audiobookshelf.log"
        log_file.parent.mkdir()
        log_file.write_text("log")
        return {
            "data_root": str(self.data_root),
            "audiobookshelf": {
                "enabled": False,
                "process_name": "Audiobookshelf",
                "config_dir": str(namespace / "config"),
                "metadata_dir": str(namespace / "metadata"),
                "runtime_dir": str(runtime_dir),
                "log_file": str(log_file),
                "release_version_enabled": False,
                "release_version": "latest",
                "branch_enabled": False,
                "branch": "master",
                "commit_sha": "",
            },
        }

    def test_audiobookshelf_remove_clears_only_owned_leaves_and_runtime(self):
        defaults = self._audiobookshelf_defaults()
        config = copy.deepcopy(defaults)
        config["audiobookshelf"]["enabled"] = True
        media = self.root / "audiobooks"
        media.mkdir()
        (media / "book.mp3").write_text("keep")
        manager = FakeConfigManager(self.root, defaults, config)

        preview = build_service_reset_preview(manager, "Audiobookshelf", "remove")
        directories = {
            target["resolved_path"]
            for target in preview["file_targets"]
            if target["kind"] == "directory"
        }
        service = defaults["audiobookshelf"]
        expected = {
            os.path.realpath(service[field])
            for field in ("config_dir", "metadata_dir", "runtime_dir")
        }
        self.assertEqual(directories, expected)
        self.assertNotIn(str(self.data_root / "audiobookshelf"), directories)

        result = execute_service_reset(
            manager, FakeProcessHandler(), "Audiobookshelf", "remove", "Audiobookshelf"
        )

        for directory in expected:
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertTrue((self.data_root / "audiobookshelf" / "unrelated.txt").exists())
        self.assertTrue((media / "book.mp3").exists())
        self.assertFalse(Path(service["log_file"]).exists())
        self.assertEqual(manager.config["audiobookshelf"], service)
        self.assertTrue(Path(result["config_backup_path"]).exists())

    def test_audiobookshelf_reset_keeps_all_files_and_resets_source_settings(self):
        defaults = self._audiobookshelf_defaults()
        config = copy.deepcopy(defaults)
        config["audiobookshelf"].update(
            enabled=True, branch_enabled=True, branch="development", commit_sha="b" * 40
        )
        manager = FakeConfigManager(self.root, defaults, config)

        result = execute_service_reset(
            manager, FakeProcessHandler(), "Audiobookshelf", "reset", "Audiobookshelf"
        )

        self.assertEqual(result["file_targets"], [])
        self.assertEqual(manager.config["audiobookshelf"], defaults["audiobookshelf"])
        self.assertTrue(
            Path(
                defaults["audiobookshelf"]["config_dir"], "absdatabase.sqlite"
            ).exists()
        )
        self.assertTrue(
            Path(defaults["audiobookshelf"]["metadata_dir"], "cover.jpg").exists()
        )
        self.assertTrue(
            Path(
                defaults["audiobookshelf"]["runtime_dir"], "incomplete-build.txt"
            ).exists()
        )

    def test_audiobookshelf_custom_directories_are_retained(self):
        defaults = self._audiobookshelf_defaults()
        config = copy.deepcopy(defaults)
        custom_paths = set()
        for field in ("config_dir", "metadata_dir", "runtime_dir"):
            custom = self.root / "custom" / field
            custom.mkdir(parents=True)
            (custom / "keep.txt").write_text("keep")
            config["audiobookshelf"][field] = str(custom)
            custom_paths.add(str(custom))
        manager = FakeConfigManager(self.root, defaults, config)

        result = execute_service_reset(
            manager, FakeProcessHandler(), "Audiobookshelf", "remove", "Audiobookshelf"
        )

        planned = {target["path"] for target in result["file_targets"]}
        self.assertFalse(custom_paths & planned)
        for custom in custom_paths:
            self.assertTrue(Path(custom, "keep.txt").exists())
            self.assertTrue(
                any(
                    custom in warning and "outside" in warning
                    for warning in result["warnings"]
                )
            )

    def test_audiobookshelf_unrecognized_runtime_is_retained(self):
        defaults = self._audiobookshelf_defaults(recognized_runtime=False)
        manager = FakeConfigManager(self.root, defaults, defaults)

        result = execute_service_reset(
            manager, FakeProcessHandler(), "Audiobookshelf", "remove", "Audiobookshelf"
        )

        runtime = defaults["audiobookshelf"]["runtime_dir"]
        self.assertNotIn(runtime, {target["path"] for target in result["file_targets"]})
        self.assertTrue(Path(runtime, "incomplete-build.txt").exists())
        self.assertTrue(
            any("recognized controller-owned" in item for item in result["warnings"])
        )

    def test_audiobookshelf_namespace_parent_is_never_a_cleanup_target(self):
        defaults = self._audiobookshelf_defaults()
        config = copy.deepcopy(defaults)
        parent = self.data_root / "audiobookshelf"
        config["audiobookshelf"]["metadata_dir"] = str(parent)
        manager = FakeConfigManager(self.root, defaults, config)

        preview = build_service_reset_preview(manager, "Audiobookshelf", "remove")

        self.assertNotIn(
            str(parent), {target["resolved_path"] for target in preview["file_targets"]}
        )
        self.assertTrue(
            any(
                str(parent) in item and "outside" in item
                for item in preview["warnings"]
            )
        )

    def test_audiobookshelf_aliases_into_unrelated_paths_are_retained(self):
        defaults = self._audiobookshelf_defaults()
        service = defaults["audiobookshelf"]
        external = self.root / "unrelated"
        external.mkdir()
        (external / "keep.txt").write_text("keep")
        runtime = Path(service["runtime_dir"])
        for child in runtime.iterdir():
            child.unlink()
        runtime.rmdir()
        runtime.symlink_to(external, target_is_directory=True)
        config_dir = Path(service["config_dir"])
        for child in config_dir.iterdir():
            child.unlink()
        config_dir.rmdir()
        config_dir.symlink_to(external, target_is_directory=True)
        manager = FakeConfigManager(self.root, defaults, defaults)

        result = execute_service_reset(
            manager, FakeProcessHandler(), "Audiobookshelf", "remove", "Audiobookshelf"
        )

        self.assertTrue((external / "keep.txt").exists())
        planned = {target["path"] for target in result["file_targets"]}
        self.assertNotIn(service["runtime_dir"], planned)
        self.assertNotIn(service["config_dir"], planned)

    def test_audiobookshelf_shared_ancestor_and_descendant_roots_are_retained(self):
        defaults = self._audiobookshelf_defaults()
        metadata = Path(defaults["audiobookshelf"]["metadata_dir"]).resolve()
        for other_path in (metadata, metadata.parent, metadata / "another-service"):
            with self.subTest(other_path=other_path):
                config = copy.deepcopy(defaults)
                config["other"] = {
                    "process_name": "Other Service",
                    "config_dir": str(other_path),
                }
                manager = FakeConfigManager(self.root, defaults, config)
                preview = build_service_reset_preview(
                    manager, "Audiobookshelf", "remove"
                )
                self.assertNotIn(
                    str(metadata),
                    {target["resolved_path"] for target in preview["file_targets"]},
                )
                self.assertTrue(
                    any("Other Service" in item for item in preview["warnings"])
                )

    def test_audiobookshelf_respects_other_service_extra_directory_ownership(self):
        defaults = self._audiobookshelf_defaults()
        service = defaults["audiobookshelf"]
        config = copy.deepcopy(defaults)
        config["other"] = {
            "process_name": "Other Service",
            "config_dir": str(self.root / "other"),
            "runtime_dir": service["runtime_dir"],
            "metadata_dir": service["metadata_dir"],
        }
        manager = FakeConfigManager(self.root, defaults, config)

        preview = build_service_reset_preview(manager, "Audiobookshelf", "remove")

        planned = {target["path"] for target in preview["file_targets"]}
        self.assertNotIn(service["runtime_dir"], planned)
        self.assertNotIn(service["metadata_dir"], planned)

    def test_other_service_cannot_remove_audiobookshelf_runtime_ancestor(self):
        defaults = self._audiobookshelf_defaults()
        other = {
            "enabled": False,
            "process_name": "Other Service",
            "config_dir": str(self.root / "opt"),
        }
        defaults["other"] = other
        manager = FakeConfigManager(self.root, defaults, defaults)

        preview = build_service_reset_preview(manager, "Other Service", "remove")

        self.assertNotIn(
            other["config_dir"], {target["path"] for target in preview["file_targets"]}
        )
        self.assertTrue(any("Audiobookshelf" in item for item in preview["warnings"]))

    def test_remove_aborts_before_backup_or_deletion_when_process_stays_alive(self):
        defaults = self._arr_defaults()
        config_dir = self.root / "sonarr" / "default"
        config_dir.mkdir(parents=True)
        retained = config_dir / "library.db"
        retained.write_text("keep")
        config = copy.deepcopy(defaults)
        config["sonarr"]["instances"]["Default"]["enabled"] = True
        manager = FakeConfigManager(self.root, defaults, config)
        handler = FailedStopProcessHandler("Sonarr")

        with mock.patch("utils.service_reset.os.getpgid", return_value=4242):
            with self.assertRaises(ServiceResetError):
                execute_service_reset(manager, handler, "Sonarr", "remove", "Sonarr")

        self.assertTrue(retained.exists())
        self.assertTrue(manager.config["sonarr"]["instances"]["Default"]["enabled"])
        self.assertFalse((self.root / "service-reset-backups").exists())


if __name__ == "__main__":
    unittest.main()
