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
