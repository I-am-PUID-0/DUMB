import asyncio
import copy
import importlib
import json
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from urllib.parse import urlsplit


def _captured_fixture(name):
    # Full discovery imports test files without the tests package prefix. Reuse
    # their captured router instead of installing the same global stubs twice.
    for module_name in (name, f"tests.{name}"):
        if module_name in sys.modules:
            return sys.modules[module_name]
    return importlib.import_module(f"tests.{name}")


# Router fixtures otherwise initialize the real global logger on a focused run.
# Keep first imports inert; full discovery retains whichever logger it captured.
_logger_stub = types.ModuleType("utils.global_logger")
_logger_stub.logger = Mock()
_logger_context = (
    nullcontext()
    if "utils.global_logger" in sys.modules
    else patch.dict(sys.modules, {"utils.global_logger": _logger_stub})
)
with _logger_context:
    process_router = _captured_fixture("test_process_update_notices").process_router
    config_router = _captured_fixture("test_config_router_helpers").config_router

    from utils import arr_postgres_migration, database_health, infinidysk_migration
    from utils import processes

ROOT = Path(__file__).resolve().parents[1]


class _ConfigManager:
    def __init__(self, root, instance):
        self.file_path = root / "dumb_config.json"
        self.config = {
            "whisparr": {"instances": {"Default": instance}},
            "dumb": {"ui": {"log_timestamp": True}},
        }
        self.schema = json.loads((ROOT / "utils/dumb_config_schema.json").read_text())
        self.saved = []
        self.file_path.write_text(json.dumps(self.config))

    def find_key_for_process(self, _process_name):
        return "whisparr", "Default"

    def get_instance(self, _instance_name, _key):
        return self.config["whisparr"]["instances"]["Default"]

    def get(self, key, default=None):
        return self.config.get(key, default)

    def save_config(self, process_name=None):
        self.saved.append(process_name)
        self.file_path.write_text(json.dumps(self.config))


class WhisparrConsumerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="dumb-whisparr-consumers-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config_dir = self.root / "data"
        self.config_dir.mkdir()
        self.instance = {
            "enabled": True,
            "process_name": "Whisparr",
            "whisparr_version": "v2",
            "config_dir": str(self.config_dir),
            "config_file": str(self.config_dir / "config.xml"),
            "port": 6969,
        }
        self._sqlite(self.config_dir / "whisparr2.db")
        self.manager = _ConfigManager(self.root, self.instance)
        self.logger = Mock()

    @staticmethod
    def _sqlite(path):
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE Probe (id INTEGER PRIMARY KEY, note TEXT)")
            connection.execute("INSERT INTO Probe VALUES (1, 'preserved fixture')")
            connection.commit()

    def _assert_unchanged(self, before, disk_before):
        self.assertEqual(self.manager.config, before)
        self.assertEqual(self.manager.file_path.read_bytes(), disk_before)
        self.assertEqual(self.manager.saved, [])

    def test_service_config_rejects_wrong_version_before_any_edit_or_save(self):
        before = copy.deepcopy(self.manager.config)
        disk_before = self.manager.file_path.read_bytes()
        request = SimpleNamespace(
            process_name="Whisparr",
            updates={"port": 6970, "whisparr_version": "v3"},
            persist=True,
        )
        with (
            patch.object(config_router, "CONFIG_MANAGER", self.manager),
            patch.object(
                config_router,
                "find_service_config",
                return_value=(self.instance, "whisparr.instances.Default"),
            ),
            patch.object(config_router, "record_config_change") as record,
        ):
            with self.assertRaises(config_router.HTTPException) as raised:
                asyncio.run(config_router.update_config(request, logger=self.logger))
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("existing v2 database", raised.exception.detail)
        self._assert_unchanged(before, disk_before)
        record.assert_not_called()

    def test_global_config_rejection_preserves_unrelated_edits_and_disk(self):
        before = copy.deepcopy(self.manager.config)
        disk_before = self.manager.file_path.read_bytes()
        request = SimpleNamespace(
            process_name=None,
            updates={
                "dumb": {"ui": {"log_timestamp": False}},
                "whisparr": {"instances": {"Default": {"whisparr_version": "v3"}}},
            },
        )
        with (
            patch.object(config_router, "CONFIG_MANAGER", self.manager),
            patch.object(config_router, "record_config_change") as record,
        ):
            with self.assertRaises(config_router.HTTPException) as raised:
                asyncio.run(config_router.update_config(request, logger=self.logger))
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("existing v2 database", raised.exception.detail)
        self._assert_unchanged(before, disk_before)
        record.assert_not_called()

    def test_onboarding_options_reject_wrong_version_before_mutation(self):
        before = copy.deepcopy(self.manager.config)
        disk_before = self.manager.file_path.read_bytes()
        with patch.object(process_router, "CONFIG_MANAGER", self.manager):
            with self.assertRaises(process_router.HTTPException) as raised:
                process_router.apply_service_options(
                    self.instance,
                    {"port": 6970, "whisparr_version": "v3"},
                    self.logger,
                    service_key="whisparr",
                )
        self.assertEqual(raised.exception.status_code, 400)
        self._assert_unchanged(before, disk_before)

    def test_onboarding_options_allow_v3_with_a_separate_empty_directory(self):
        separate = self.root / "eros"
        separate.mkdir()
        self.instance.update(
            config_dir=str(separate), config_file=str(separate / "config.xml")
        )
        with patch.object(process_router, "CONFIG_MANAGER", self.manager):
            process_router.apply_service_options(
                self.instance,
                {"port": 6970, "whisparr_version": "v3"},
                self.logger,
                service_key="whisparr",
            )
        self.assertEqual(self.instance["whisparr_version"], "v3")
        self.assertEqual(self.instance["port"], 6970)
        self.assertEqual(self.manager.saved, [None])
        self.assertTrue((self.config_dir / "whisparr2.db").is_file())
        self.assertEqual(list(separate.iterdir()), [])

    def test_restart_rejection_precedes_protection_and_stopping(self):
        self.instance["whisparr_version"] = "v3"
        handler, updater, protection = Mock(), Mock(), Mock()
        request = SimpleNamespace(process_name="Whisparr", protection_override=False)
        with patch.object(process_router, "CONFIG_MANAGER", self.manager):
            with self.assertRaises(process_router.HTTPException) as raised:
                asyncio.run(
                    process_router.restart_service(
                        request,
                        process_handler=handler,
                        updater=updater,
                        logger=self.logger,
                        api_state=Mock(),
                        media_protection=protection,
                    )
                )
        self.assertEqual(raised.exception.status_code, 400)
        protection.begin_planned.assert_not_called()
        handler.stop_process.assert_not_called()
        updater.auto_update.assert_not_called()

    def test_onboarding_metadata_advertises_version_for_each_instance(self):
        template = json.loads((ROOT / "utils/dumb_config.json").read_text())
        template_path = self.root / "template.json"
        template_path.write_text(json.dumps({"whisparr": template["whisparr"]}))
        manager = SimpleNamespace(default_config_path=str(template_path))
        with patch.object(process_router, "CONFIG_MANAGER", manager):
            result = asyncio.run(process_router.get_core_services(logger=self.logger))
        metadata = next(
            service
            for service in result["core_services"]
            if service["key"] == "whisparr"
        )
        self.assertEqual(
            metadata["service_options"]["whisparr"]["whisparr_version"], ""
        )
        for options in metadata["instance_options"].values():
            self.assertEqual(options["whisparr_version"], "")
        description = metadata["service_option_descriptions"]["whisparr"][
            "whisparr_version"
        ]
        self.assertIn("v2", description)
        self.assertIn("v3", description)

    def test_health_and_migration_select_existing_version_database_read_only(self):
        collector = database_health.DatabaseHealthCollector()
        for name, selected in [
            ("whisparr.db", "v2"),
            ("whisparr2.db", ""),
            ("whisparr3.db", ""),
        ]:
            with self.subTest(database=name):
                directory = self.root / name.replace(".db", "")
                directory.mkdir()
                main = directory / name
                logs = directory / "logs.db"
                self._sqlite(main)
                self._sqlite(logs)
                before = {path.name: path.read_bytes() for path in directory.iterdir()}
                instance = {"config_dir": str(directory), "whisparr_version": selected}
                health_paths = collector._sqlite_paths(
                    {"config_key": "whisparr", "service_config": instance}
                )
                migration_paths = arr_postgres_migration._source_paths(
                    "whisparr", instance
                )
                self.assertEqual(
                    health_paths, [("main", str(main)), ("logs", str(logs))]
                )
                self.assertEqual(migration_paths["main"], main)
                self.assertEqual(migration_paths["log"], logs)
                self.assertEqual(
                    {path.name: path.read_bytes() for path in directory.iterdir()},
                    before,
                )

    def test_configured_start_rejects_prepared_wrong_major_without_popen(self):
        binary_dir = self.root / "prepared"
        binary_dir.mkdir()
        prepared = binary_dir / "Whisparr"
        prepared.touch()
        prepared.with_name("Whisparr.Core.dll").write_bytes(
            b"Whisparr.Common, Version=3.6.4.1865, Culture=neutral"
        )
        self.instance["command"] = [str(prepared), "--nobrowser"]
        handler = object.__new__(processes.ProcessHandler)
        handler.logger = self.logger
        handler.setup_tracker = {"Whisparr"}
        handler.setup_tracker_lock = threading.Lock()
        handler.process_names = {}
        handler._prefixed_name = Mock(return_value="Whisparr")
        handler._set_restart_disabled = Mock()
        handler._reset_healthcheck_state = Mock()
        with (
            patch.object(processes, "CONFIG_MANAGER", self.manager),
            patch.object(processes, "notify_event"),
            patch.object(processes.subprocess, "Popen") as popen,
        ):
            success, error = handler._start_process_admitted(
                "Whisparr",
                config_dir=str(self.config_dir),
                command=[str(self.root / "old" / "Whisparr")],
            )
        self.assertFalse(success)
        self.assertIn("Installed Whisparr 3.6.4.1865", error)
        self.assertIn("selected v2", error)
        self.assertEqual(handler.setup_tracker, {"Whisparr"})
        popen.assert_not_called()

    def test_namespace_inventory_uses_the_selected_version_item_endpoint(self):
        for version, endpoint in [("v2", "series"), ("v3", "movie")]:
            with self.subTest(version=version):
                calls = []
                item = {"id": 17, "path": "/library/original/item"}

                def request(url, _key, method="GET", data=None):
                    calls.append((urlsplit(url).path, method))
                    if urlsplit(url).path.endswith("/queue"):
                        return {"records": [], "totalRecords": 0}
                    return [item] if urlsplit(url).path.endswith("/" + endpoint) else []

                target = {
                    "service_key": "whisparr",
                    "instance_name": "Default",
                    "process_name": "Whisparr",
                    "config": {"whisparr_version": version, "port": 6969},
                }
                with (
                    patch.object(
                        infinidysk_migration.InfiniDyskMigrationManager,
                        "_process_running",
                        return_value=True,
                    ),
                    patch.object(
                        infinidysk_migration,
                        "_parse_arr_api_key",
                        return_value="test-key",
                    ),
                    patch.object(
                        infinidysk_migration, "_migration_arr_req", side_effect=request
                    ),
                ):
                    snapshot, blockers = (
                        infinidysk_migration.InfiniDyskMigrationManager._arr_target_snapshot(
                            target, Mock()
                        )
                    )
                self.assertEqual(blockers, [])
                self.assertEqual(snapshot["items"], [item])
                self.assertEqual(snapshot["item_endpoint"], endpoint)
                self.assertIn((f"/api/v3/{endpoint}", "GET"), calls)
                other = "series" if endpoint == "movie" else "movie"
                self.assertNotIn((f"/api/v3/{other}", "GET"), calls)
                self.assertTrue(all(method == "GET" for _, method in calls))

    def test_namespace_bulk_editor_preserves_v2_and_uses_movie_ids_for_v3(self):
        for endpoint, id_field in [("series", "seriesIds"), ("movie", "movieIds")]:
            with self.subTest(endpoint=endpoint):
                snapshot = {
                    "service_key": "whisparr",
                    "host": "http://127.0.0.1:6969",
                    "api_key": "test-key",
                    "api_version": "v3",
                    "item_endpoint": endpoint,
                    "roots": [{"id": 1, "path": "/library/original"}],
                    "items": [{"id": 17, "path": "/library/original/item"}],
                }
                desired = {
                    "roots": [{"id": 1, "path": "/library/replaced"}],
                    "items": [{"id": 17, "path": "/library/replaced/item"}],
                }
                with patch.object(
                    infinidysk_migration, "_migration_arr_req"
                ) as request:
                    changed = infinidysk_migration.InfiniDyskMigrationManager._update_arr_items(
                        snapshot, desired
                    )
                self.assertEqual(changed, 1)
                self.assertEqual(
                    request.call_args_list,
                    [
                        call(
                            f"http://127.0.0.1:6969/api/v3/{endpoint}/editor",
                            "test-key",
                            "PUT",
                            {
                                id_field: [17],
                                "rootFolderPath": "/library/replaced",
                                "moveFiles": False,
                            },
                        )
                    ],
                )


if __name__ == "__main__":
    unittest.main()
