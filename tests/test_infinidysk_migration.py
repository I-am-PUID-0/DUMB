import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from utils.infinidysk_migration import (
    ARR_EDITOR_BATCH_SIZE,
    ARR_INVENTORY_TIMEOUT_SECONDS,
    ARR_SERVICE_API,
    InfiniDyskMigrationError,
    InfiniDyskMigrationManager,
    QUIESCE_TIMEOUT_SECONDS,
    _config_fingerprint,
    _migration_arr_req,
    _safe_error_detail,
    _replace_namespace_text,
    _rewrite_config_namespace,
)

ROOT = Path(__file__).resolve().parents[1]


def copy_json(value):
    return json.loads(json.dumps(value))


def legacy_config():
    return {
        "dumb": {
            "ui": {
                "sidebar": {
                    "service_order": ["NzbDAV", "Radarr NzbDAV", "Plex"],
                    "service_shortcuts": {
                        "alt+i": "NzbDAV",
                        "alt+r": "Radarr NzbDAV",
                    },
                }
            },
            "notifications": {
                "destinations": [{"service_names": ["NzbDAV", "Radarr NzbDAV", "Plex"]}]
            },
        },
        "infinidysk": {
            "enabled": True,
            "process_name": "NzbDAV",
            "repo_owner": "nzbdav",
            "repo_name": "nzbdav",
            "config_dir": "/nzbdav",
            "symlink_backup_roots": ["/mnt/debrid/nzbdav-symlinks"],
        },
        "radarr": {
            "instances": {
                "NzbDAV": {
                    "enabled": True,
                    "core_service": "infinidysk",
                    "process_name": "Radarr NzbDAV",
                }
            }
        },
    }


class InfiniDyskMigrationTests(unittest.TestCase):
    def test_whisparr_inventory_uses_current_series_endpoint(self):
        self.assertEqual(("v3", "series"), ARR_SERVICE_API["whisparr"])

    def test_migration_arr_requests_use_catalog_sized_timeout(self):
        with patch("utils.infinidysk_migration._arr_req") as request:
            _migration_arr_req("http://127.0.0.1:8990/api/v3/series", "secret")

        request.assert_called_once_with(
            "http://127.0.0.1:8990/api/v3/series",
            "secret",
            "GET",
            None,
            timeout=ARR_INVENTORY_TIMEOUT_SECONDS,
        )

    def test_migration_http_error_reports_sanitized_operation_and_response(self):
        error = urllib.error.HTTPError(
            "http://127.0.0.1:8990/api/v3/rootfolder/7?apiKey=secret",
            409,
            "Conflict",
            {},
            None,
        )
        error.body = json.dumps({"message": "Root folder is still in use"})

        with (
            patch("utils.infinidysk_migration._arr_req", side_effect=error),
            self.assertRaises(urllib.error.HTTPError) as raised,
        ):
            _migration_arr_req(
                error.url,
                "secret",
                "DELETE",
            )

        detail = _safe_error_detail(raised.exception)
        self.assertEqual(
            "Arr API DELETE http://127.0.0.1:8990/api/v3/rootfolder/7 "
            "returned HTTP 409 Conflict: Root folder is still in use",
            detail,
        )
        self.assertNotIn("apiKey", detail)
        self.assertNotIn("secret", detail)

    def test_preflight_reports_transient_activity_without_blocking_job_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = InfiniDyskMigrationManager(Path(temp_dir) / "state.json")
            config_manager = MagicMock()
            config_manager.config = {
                "infinidysk": {"enabled": True, "process_name": "NzbDAV"}
            }
            arr = [{"process_name": "Sonarr NzbDAV", "queue_count": 3}]
            media = [
                {
                    "service_key": "plex",
                    "process_name": "Plex",
                    "libraries": [],
                    "activity": {"state": "busy", "active_sessions": 1},
                }
            ]
            with (
                patch("utils.infinidysk_migration.CONFIG_MANAGER", config_manager),
                patch.object(
                    manager, "_legacy_paths", return_value=[{"value": "/nzbdav"}]
                ),
                patch.object(
                    manager, "_namespace_filesystem_plan", return_value=([], [])
                ),
                patch.object(manager, "_linked_service_inventory", return_value=[]),
                patch.object(manager, "_arr_snapshot", return_value=(arr, [], [])),
                patch.object(manager, "_prowlarr_snapshot", return_value=([], [])),
                patch.object(manager, "_media_snapshot", return_value=(media, [])),
                patch.object(
                    manager, "_infinidysk_active_reads", return_value=(2, None)
                ),
            ):
                result = manager.preflight(MagicMock(), MagicMock())

        self.assertTrue(result["ready"])
        self.assertEqual(3, len(result["pending_conditions"]))
        self.assertIn("3 queued", result["pending_conditions"][0])
        self.assertIn("media activity", result["pending_conditions"][1])
        self.assertIn("2 active read", result["pending_conditions"][2])

    def test_health_check_config_update_is_read_back_and_verified(self):
        manager = InfiniDyskMigrationManager()
        config = {
            "infinidysk": {
                "env": {"FRONTEND_BACKEND_API_KEY": "secret"},
                "backend_port": 8080,
            }
        }
        with patch.object(
            InfiniDyskMigrationManager,
            "_infinidysk_config_api_request",
            side_effect=[
                {"status": True},
                {
                    "configItems": [
                        {
                            "configName": "repair.enable",
                            "configValue": "false",
                            "environmentVariableName": None,
                        }
                    ]
                },
            ],
        ) as request:
            manager._set_infinidysk_config_item(config, "repair.enable", "false")

        self.assertEqual(
            [
                call(config, "update-config", {"repair.enable": "false"}),
                call(config, "get-config", {"config-keys": "repair.enable"}),
            ],
            request.call_args_list,
        )

    def test_health_check_scheduler_is_paused_and_exact_value_is_restored(self):
        manager = InfiniDyskMigrationManager()
        config = {
            "infinidysk": {
                "enabled": True,
                "process_name": "NzbDAV",
            }
        }
        process = MagicMock()
        process.poll.return_value = None
        handler = MagicMock()
        handler.process_names = {"NzbDAV": process}
        handler._prefixed_name.side_effect = lambda value: value

        with (
            patch.object(
                InfiniDyskMigrationManager,
                "_infinidysk_config_item",
                return_value={
                    "present": True,
                    "value": "true",
                    "environment_variable_name": "",
                },
            ),
            patch.object(
                InfiniDyskMigrationManager, "_set_infinidysk_config_item"
            ) as update,
        ):
            guard = manager._capture_infinidysk_health_check_guard(config, handler)
            manager._pause_infinidysk_health_checks(config, guard)
            manager._restore_infinidysk_health_checks(config, handler, guard)

        self.assertEqual(
            [
                call(config, "repair.enable", "false"),
                call(config, "repair.enable", "true"),
            ],
            update.call_args_list,
        )
        self.assertTrue(guard["paused"])
        self.assertTrue(guard["changed"])
        self.assertTrue(guard["restored"])

    def test_environment_managed_health_check_scheduler_blocks_safe_pause(self):
        manager = InfiniDyskMigrationManager()
        guard = {
            "applicable": True,
            "original_value": "true",
            "environment_variable_name": "NZBDAV_CONFIG__REPAIR__ENABLE",
        }

        with (
            patch.object(
                InfiniDyskMigrationManager, "_set_infinidysk_config_item"
            ) as update,
            self.assertRaisesRegex(
                InfiniDyskMigrationError,
                "NZBDAV_CONFIG__REPAIR__ENABLE",
            ),
        ):
            manager._pause_infinidysk_health_checks({}, guard)

        update.assert_not_called()

    def test_preflight_blocks_environment_managed_health_check_scheduler(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = InfiniDyskMigrationManager(Path(temp_dir) / "state.json")
            config_manager = MagicMock()
            config_manager.config = {
                "infinidysk": {
                    "enabled": True,
                    "process_name": "NzbDAV",
                    "config_dir": "/nzbdav",
                }
            }
            process = MagicMock()
            process.poll.return_value = None
            handler = MagicMock()
            handler.process_names = {"NzbDAV": process}
            handler._prefixed_name.side_effect = lambda value: value
            with (
                patch("utils.infinidysk_migration.CONFIG_MANAGER", config_manager),
                patch.object(
                    manager, "_legacy_paths", return_value=[{"value": "/nzbdav"}]
                ),
                patch.object(
                    manager, "_namespace_filesystem_plan", return_value=([], [])
                ),
                patch.object(manager, "_linked_service_inventory", return_value=[]),
                patch.object(manager, "_arr_snapshot", return_value=([], [], [])),
                patch.object(manager, "_prowlarr_snapshot", return_value=([], [])),
                patch.object(manager, "_media_snapshot", return_value=([], [])),
                patch.object(
                    manager, "_infinidysk_active_reads", return_value=(0, None)
                ),
                patch.object(
                    manager,
                    "_infinidysk_config_item",
                    return_value={
                        "present": True,
                        "value": "true",
                        "environment_variable_name": ("NZBDAV_CONFIG__REPAIR__ENABLE"),
                    },
                ),
            ):
                result = manager.preflight(handler, MagicMock())

        self.assertFalse(result["ready"])
        self.assertTrue(
            any(
                "NZBDAV_CONFIG__REPAIR__ENABLE" in blocker
                for blocker in result["blockers"]
            )
        )

    def test_arr_discovery_includes_live_legacy_references_without_core_link(self):
        manager = InfiniDyskMigrationManager()
        config = {
            "sonarr": {
                "instances": {
                    "Anime": {
                        "enabled": True,
                        "process_name": "Sonarr Anime",
                    },
                    "Main": {
                        "enabled": True,
                        "process_name": "Sonarr Main",
                    },
                }
            }
        }

        def snapshot(target, _handler):
            legacy = target["instance_name"] == "Anime"
            return (
                {
                    "service_key": "sonarr",
                    "instance_name": target["instance_name"],
                    "process_name": target["process_name"],
                    "host": "http://127.0.0.1:8990",
                    "api_version": "v3",
                    "item_endpoint": "series",
                    "api_key": "secret",
                    "roots": [
                        {
                            "id": 1,
                            "path": (
                                "/mnt/debrid/nzbdav-symlinks/anime"
                                if legacy
                                else "/media/tv"
                            ),
                        }
                    ],
                    "items": [],
                    "clients": [],
                    "tags": [],
                    "queue_count": 0,
                },
                [],
            )

        with patch.object(
            InfiniDyskMigrationManager,
            "_arr_target_snapshot",
            side_effect=snapshot,
        ):
            snapshots, blockers, discovery = manager._arr_snapshot(config, MagicMock())

        self.assertEqual([], blockers)
        self.assertEqual(["Sonarr Anime"], [item["process_name"] for item in snapshots])
        by_process = {item["process_name"]: item for item in discovery}
        self.assertTrue(by_process["Sonarr Anime"]["included"])
        self.assertIn(
            "legacy root-folder reference",
            " ".join(by_process["Sonarr Anime"]["reasons"]),
        )
        self.assertFalse(by_process["Sonarr Main"]["included"])

    def test_radarr_inventory_captures_import_lists_and_collections(self):
        manager = InfiniDyskMigrationManager()
        target = {
            "service_key": "radarr",
            "instance_name": "InfiniDysk",
            "process_name": "Radarr InfiniDysk",
            "config": {
                "port": 7878,
                "config_file": "/radarr/infinidysk/config.xml",
            },
        }

        def request(url, _token, method="GET", data=None):
            self.assertEqual("GET", method)
            self.assertIsNone(data)
            endpoint = url.split("/api/v3/", 1)[1]
            if endpoint.startswith("queue?"):
                return {"totalRecords": 0, "records": []}
            responses = {
                "rootfolder": [],
                "movie": [],
                "downloadclient": [],
                "importlist": [
                    {
                        "id": 4,
                        "rootFolderPath": "/mnt/debrid/nzbdav-symlinks/movies",
                    }
                ],
                "collection": [
                    {
                        "id": 5,
                        "rootFolderPath": "/mnt/debrid/nzbdav-symlinks/movies",
                    }
                ],
                "tag": [],
            }
            return responses[endpoint]

        with (
            patch.object(
                InfiniDyskMigrationManager, "_process_running", return_value=True
            ),
            patch(
                "utils.infinidysk_migration._parse_arr_api_key",
                return_value="secret",
            ),
            patch(
                "utils.infinidysk_migration._migration_arr_req",
                side_effect=request,
            ),
        ):
            snapshot, blockers = manager._arr_target_snapshot(target, MagicMock())

        self.assertEqual([], blockers)
        self.assertEqual(4, snapshot["import_lists"][0]["id"])
        self.assertEqual(5, snapshot["collections"][0]["id"])

    def test_media_snapshot_infers_external_plex_from_global_credentials(self):
        manager = InfiniDyskMigrationManager()
        adapter = MagicMock()
        adapter.server_identity.return_value = {
            "name": "Migration Plex",
            "machine_identifier": "machine-1",
            "version": "1.2.3",
        }
        adapter.activity.return_value = {"state": "idle", "active_sessions": 0}
        adapter.library_paths.return_value = [
            {
                "id": "1",
                "name": "Movies",
                "paths": ["/mnt/debrid/nzbdav-symlinks/movies"],
            }
        ]

        with patch(
            "utils.infinidysk_migration.build_adapter", return_value=adapter
        ) as build:
            snapshots, blockers = manager._media_snapshot(
                {
                    "dumb": {
                        "plex_address": "http://plex.example.invalid:32400",
                        "plex_token": "secret",
                    },
                    "plex": {"enabled": False, "port": 32400},
                    "jellyfin": {"enabled": False},
                    "emby": {"enabled": False},
                },
                MagicMock(),
                MagicMock(),
            )

        self.assertEqual([], blockers)
        build.assert_called_once_with("plex", "External Plex", unittest.mock.ANY)
        self.assertTrue(snapshots[0]["external_api_only"])
        self.assertEqual("Migration Plex", snapshots[0]["identity"]["name"])

    def test_external_plex_is_guarded_but_never_stopped_by_quiescence(self):
        manager = InfiniDyskMigrationManager()
        running_names = {"NzbDAV"}

        class Process:
            def __init__(self, name):
                self.name = name

            def poll(self):
                return None if self.name in running_names else 0

        handler = MagicMock()
        handler.process_names = {"NzbDAV": Process("NzbDAV")}
        handler._prefixed_name.side_effect = lambda value: value
        handler.stop_process.side_effect = lambda name: running_names.discard(name)
        adapter = MagicMock()
        adapter.activity.return_value = {"state": "idle", "active_sessions": 0}
        adapter.library_paths.return_value = [
            {
                "id": "1",
                "name": "Movies",
                "paths": ["/mnt/debrid/nzbdav-symlinks/movies"],
            }
        ]
        adapter.enter_scan_guard.return_value = {"settings": {}}
        stopped = []
        scan_guards = []

        with (
            patch("utils.infinidysk_migration.build_adapter", return_value=adapter),
            patch.object(manager, "_infinidysk_active_reads", return_value=(0, None)),
        ):
            refreshed = manager._quiesce_for_cutover(
                {"infinidysk": {"enabled": True, "process_name": "NzbDAV"}},
                {
                    "arr": [],
                    "prowlarr": [],
                    "media": [
                        {
                            "service_key": "plex",
                            "process_name": "External Plex",
                            "external_api_only": True,
                            "identity": {"machine_identifier": "machine-1"},
                        }
                    ],
                },
                ["NzbDAV", "External Plex"],
                handler,
                MagicMock(),
                MagicMock(),
                stopped,
                scan_guards,
            )

        self.assertEqual(
            ["NzbDAV"], [item.args[0] for item in handler.stop_process.call_args_list]
        )
        self.assertEqual(["NzbDAV"], stopped)
        self.assertTrue(refreshed["media"][0]["external_api_only"])
        self.assertEqual(
            "machine-1", refreshed["media"][0]["identity"]["machine_identifier"]
        )
        self.assertEqual(1, len(scan_guards))

    def test_external_plex_library_update_refuses_changed_server_identity(self):
        adapter = MagicMock()
        adapter.server_identity.return_value = {
            "machine_identifier": "different-machine"
        }

        with patch("utils.infinidysk_migration.build_adapter", return_value=adapter):
            with self.assertRaisesRegex(
                RuntimeError, "server identity changed after preflight"
            ):
                InfiniDyskMigrationManager._apply_media_snapshot(
                    {
                        "service_key": "plex",
                        "process_name": "External Plex",
                        "identity": {"machine_identifier": "expected-machine"},
                    },
                    {"libraries": []},
                    MagicMock(),
                )

        adapter.replace_library_paths.assert_not_called()

    def test_quiescence_stops_producer_then_latches_arr_before_provider(self):
        manager = InfiniDyskMigrationManager()
        config = {
            "infinidysk": {"enabled": True, "process_name": "NzbDAV"},
            "sonarr": {
                "instances": {
                    "NzbDAV": {
                        "enabled": True,
                        "core_service": "infinidysk",
                        "process_name": "Sonarr NzbDAV",
                    }
                }
            },
            "neutarr": {
                "instances": {
                    "NzbDAV": {
                        "enabled": True,
                        "core_service": "infinidysk",
                        "process_name": "NeutArr NzbDAV",
                    }
                }
            },
        }
        running_names = {"NzbDAV", "Sonarr NzbDAV", "NeutArr NzbDAV"}

        class Process:
            def __init__(self, name):
                self.name = name

            def poll(self):
                return None if self.name in running_names else 0

        handler = MagicMock()
        handler.process_names = {name: Process(name) for name in running_names}
        handler._prefixed_name.side_effect = lambda value: value
        handler.stop_process.side_effect = lambda name: running_names.discard(name)
        preflight = {
            "arr": [
                {
                    "process_name": "Sonarr NzbDAV",
                    "host": "http://127.0.0.1:8990",
                    "api_version": "v3",
                    "api_key": "secret",
                }
            ],
            "prowlarr": [],
            "media": [],
        }
        final_snapshot = {
            **preflight["arr"][0],
            "service_key": "sonarr",
            "instance_name": "NzbDAV",
            "item_endpoint": "series",
            "queue_count": 0,
            "roots": [],
            "items": [],
            "clients": [],
            "tags": [],
        }
        stopped = []
        with (
            patch(
                "utils.infinidysk_migration._migration_arr_req",
                side_effect=[
                    {"totalRecords": 1, "records": [{}]},
                    {"totalRecords": 0, "records": []},
                    {"totalRecords": 0, "records": []},
                ],
            ),
            patch.object(
                manager,
                "_arr_target_snapshot",
                return_value=(final_snapshot, []),
            ),
            patch.object(manager, "_infinidysk_active_reads", return_value=(0, None)),
            patch("utils.infinidysk_migration.time.sleep", return_value=None),
        ):
            refreshed = manager._quiesce_for_cutover(
                config,
                preflight,
                ["NzbDAV", "Sonarr NzbDAV", "NeutArr NzbDAV"],
                handler,
                MagicMock(),
                MagicMock(),
                stopped,
                [],
            )

        self.assertEqual(
            ["NeutArr NzbDAV", "Sonarr NzbDAV", "NzbDAV"],
            [item.args[0] for item in handler.stop_process.call_args_list],
        )
        self.assertEqual(["NzbDAV", "Sonarr NzbDAV", "NeutArr NzbDAV"], stopped)
        self.assertEqual(0, refreshed["arr"][0]["queue_count"])
        self.assertEqual([], refreshed["pending_conditions"])

    def test_quiescence_waits_for_transient_arr_queue_api_failure(self):
        manager = InfiniDyskMigrationManager()
        config = {
            "infinidysk": {"enabled": True, "process_name": "NzbDAV"},
            "radarr": {
                "instances": {
                    "NzbDAV": {
                        "enabled": True,
                        "core_service": "infinidysk",
                        "process_name": "Radarr NzbDAV",
                    }
                }
            },
        }
        running_names = {"NzbDAV", "Radarr NzbDAV"}

        class Process:
            def __init__(self, name):
                self.name = name

            def poll(self):
                return None if self.name in running_names else 0

        handler = MagicMock()
        handler.process_names = {name: Process(name) for name in running_names}
        handler._prefixed_name.side_effect = lambda value: value
        handler.stop_process.side_effect = lambda name: running_names.discard(name)
        original = {
            "process_name": "Radarr NzbDAV",
            "host": "http://127.0.0.1:7879",
            "api_version": "v3",
            "api_key": "secret",
        }
        final_snapshot = {
            **original,
            "service_key": "radarr",
            "instance_name": "NzbDAV",
            "item_endpoint": "movie",
            "queue_count": 0,
            "roots": [],
            "items": [],
            "clients": [],
            "tags": [],
        }
        progress = MagicMock()

        with (
            patch(
                "utils.infinidysk_migration._migration_arr_req",
                side_effect=[
                    RuntimeError("temporary PostgreSQL saturation"),
                    {"totalRecords": 0, "records": []},
                    {"totalRecords": 0, "records": []},
                ],
            ),
            patch.object(
                manager, "_arr_target_snapshot", return_value=(final_snapshot, [])
            ),
            patch.object(manager, "_infinidysk_active_reads", return_value=(0, None)),
            patch("utils.infinidysk_migration.time.sleep", return_value=None),
        ):
            refreshed = manager._quiesce_for_cutover(
                config,
                {"arr": [original], "prowlarr": [], "media": []},
                ["NzbDAV", "Radarr NzbDAV"],
                handler,
                MagicMock(),
                progress,
                [],
                [],
            )

        self.assertEqual(0, refreshed["arr"][0]["queue_count"])
        self.assertTrue(
            any(
                "Waiting for Arr APIs and their databases to recover"
                in str(call.args[1])
                for call in progress.call_args_list
            )
        )

    def test_quiescence_timeout_refuses_cutover_before_stopping_provider(self):
        manager = InfiniDyskMigrationManager()
        handler = MagicMock()
        handler.process_names = {}
        handler._prefixed_name.side_effect = lambda value: value

        with (
            patch.object(manager, "_infinidysk_active_reads", return_value=(1, None)),
            patch(
                "utils.infinidysk_migration.time.monotonic",
                side_effect=[100.0, 100.0 + QUIESCE_TIMEOUT_SECONDS],
            ),
        ):
            with self.assertRaisesRegex(
                InfiniDyskMigrationError,
                "timed out after one hour before any namespace paths were moved",
            ):
                manager._quiesce_for_cutover(
                    {
                        "infinidysk": {
                            "enabled": True,
                            "process_name": "NzbDAV",
                        }
                    },
                    {"arr": [], "prowlarr": [], "media": []},
                    ["NzbDAV"],
                    handler,
                    MagicMock(),
                    MagicMock(),
                    [],
                    [],
                )

        handler.stop_process.assert_not_called()

    def test_operator_can_stop_active_playback_without_bypassing_read_drain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = InfiniDyskMigrationManager(Path(temp_dir) / "state.json")
            job_id = "a" * 32
            manager._save_job(
                {
                    "job_id": job_id,
                    "status": "running",
                    "stage": "quiescing",
                    "message": "Waiting for playback.",
                    "progress": 24,
                    "events": [],
                    "worker_id": manager._worker_id,
                }
            )
            manager._reset_playback_override(job_id)
            manager._set_playback_override_availability(job_id, ["Plex"])

            requested = manager.request_playback_stop(job_id)

            self.assertTrue(requested["playback_stop_requested"])
            self.assertFalse(requested["playback_override_available"])
            self.assertEqual(["Plex"], requested["active_media_servers"])

            running_names = {"NzbDAV", "Plex"}

            class Process:
                def __init__(self, name):
                    self.name = name

                def poll(self):
                    return None if self.name in running_names else 0

            handler = MagicMock()
            handler.process_names = {name: Process(name) for name in running_names}
            handler._prefixed_name.side_effect = lambda value: value
            handler.stop_process.side_effect = lambda name: running_names.discard(name)
            adapter = MagicMock()
            adapter.activity.return_value = {
                "state": "busy",
                "active_sessions": 1,
            }
            adapter.library_paths.return_value = [
                {"id": "1", "path": "/mnt/debrid/nzbdav-symlinks/movies"}
            ]
            adapter.enter_scan_guard.return_value = {"guarded": True}
            progress = MagicMock()
            scan_guards = []
            stopped = []

            with (
                patch("utils.infinidysk_migration.build_adapter", return_value=adapter),
                patch.object(
                    manager,
                    "_infinidysk_active_reads",
                    side_effect=[(1, None), (0, None)],
                ),
                patch("utils.infinidysk_migration.time.sleep", return_value=None),
            ):
                refreshed = manager._quiesce_for_cutover(
                    {
                        "infinidysk": {
                            "enabled": True,
                            "process_name": "NzbDAV",
                        }
                    },
                    {
                        "arr": [],
                        "prowlarr": [],
                        "media": [{"service_key": "plex", "process_name": "Plex"}],
                    },
                    ["NzbDAV", "Plex"],
                    handler,
                    MagicMock(),
                    progress,
                    stopped,
                    scan_guards,
                    job_id=job_id,
                )

        self.assertEqual(
            ["Plex", "NzbDAV"],
            [item.args[0] for item in handler.stop_process.call_args_list],
        )
        self.assertEqual(["NzbDAV", "Plex"], stopped)
        self.assertTrue(refreshed["media"][0]["playback_interrupted"])
        self.assertEqual(
            [{"guarded": True}], [item["snapshot"] for item in scan_guards]
        )
        self.assertTrue(
            any(
                "Operator approved playback interruption" in item.args[1]
                for item in progress.call_args_list
            )
        )

    def test_primary_service_restart_name_stays_canonical_when_attached_names_change(
        self,
    ):
        renamed = InfiniDyskMigrationManager._renamed_processes(
            [
                "NzbDAV Migration Lab",
                "Rclone w/ NzbDAV Migration Lab",
                "Radarr NzbDAV Migration Lab",
            ],
            "NzbDAV Migration Lab",
            True,
        )

        self.assertEqual("InfiniDysk", renamed["NzbDAV Migration Lab"])
        self.assertEqual(
            "Rclone w/ InfiniDysk Migration Lab",
            renamed["Rclone w/ NzbDAV Migration Lab"],
        )
        self.assertEqual(
            "Radarr InfiniDysk Migration Lab",
            renamed["Radarr NzbDAV Migration Lab"],
        )

    def test_rollback_restart_skips_setup_for_restored_services(self):
        handler = MagicMock()
        handler.setup_tracker = set()
        handler.setup_tracker_lock = threading.Lock()
        handler.start_process.return_value = (True, None)

        InfiniDyskMigrationManager._start_processes(
            handler, ["NzbDAV Migration Lab"], force_setup=False
        )

        self.assertIn("NzbDAV Migration Lab", handler.setup_tracker)
        handler.start_process.assert_called_once_with("NzbDAV Migration Lab")

    def test_rollback_restart_continues_after_one_service_fails(self):
        handler = MagicMock()
        handler.setup_tracker = set()
        handler.setup_tracker_lock = threading.Lock()
        handler.start_process.side_effect = [
            (False, "InfiniDysk failed to stay running"),
            (True, None),
            (True, None),
        ]
        process_names = ["InfiniDysk", "Sonarr 1080p", "Prowlarr"]

        errors = InfiniDyskMigrationManager._start_processes(
            handler,
            process_names,
            force_setup=False,
            continue_on_error=True,
        )

        self.assertEqual(["InfiniDysk: InfiniDysk failed to stay running"], errors)
        self.assertEqual(
            [call(name) for name in process_names],
            handler.start_process.call_args_list,
        )
        self.assertEqual(set(process_names), handler.setup_tracker)

    def test_guarded_prowlarr_restart_skips_preemptive_integration_setup(self):
        handler = MagicMock()
        handler.setup_tracker = set()
        handler.setup_tracker_lock = threading.Lock()
        handler.start_process.return_value = (True, None)

        InfiniDyskMigrationManager._start_prowlarr_for_migration(handler, ["Prowlarr"])

        self.assertIn("Prowlarr", handler.setup_tracker)
        handler.start_process.assert_called_once_with("Prowlarr")

    def test_full_namespace_job_persists_progress_and_terminal_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = InfiniDyskMigrationManager(Path(temp_dir) / "state.json")
            config = {"infinidysk": {"enabled": True}}
            token = "preflight-token"
            manager._save_state(
                {
                    "preflight": {
                        "token": token,
                        "expires_at": int(time.time()) + 300,
                        "blockers": [],
                        "config_fingerprint": _config_fingerprint(config),
                    }
                }
            )
            updater = MagicMock()
            updater.updating = threading.Lock()

            def apply_job(
                preflight_token,
                rename_attached_services,
                process_handler,
                logger,
                job_id=None,
                progress_callback=None,
            ):
                self.assertEqual(token, preflight_token)
                self.assertEqual(32, len(job_id))
                progress_callback("filesystem", "Moving paths.", 38)
                progress_callback("validation", "Validating results.", 97)
                return {"status": "completed", "message": "Migration complete."}

            config_manager = MagicMock()
            config_manager.config = config
            with (
                patch("utils.infinidysk_migration.CONFIG_MANAGER", config_manager),
                patch.object(manager, "apply_full_namespace", side_effect=apply_job),
            ):
                started = manager.start_full_namespace_job(
                    token, True, MagicMock(), MagicMock(), updater
                )
                manager._active_job_thread.join(timeout=3)

            self.assertFalse(manager._active_job_thread.is_alive())
            persisted = manager.get_job(started["job_id"])
            self.assertEqual("completed", persisted["status"])
            self.assertEqual(100, persisted["progress"])
            self.assertEqual("completed", persisted["result"]["status"])
            self.assertIn(
                "filesystem", [event["stage"] for event in persisted["events"]]
            )

    def test_preflight_inventory_and_job_are_persisted_in_private_sidecars(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            manager = InfiniDyskMigrationManager(state_path)
            manager._save_state(
                {
                    "status": "pending",
                    "preflight": {"legacy": "x" * 1024},
                    "job": {"status": "running"},
                }
            )
            preflight = {
                "token": "token",
                "arr": [{"items": [{"id": 1, "path": "/example"}]}],
            }
            job = {
                "job_id": "a" * 32,
                "status": "completed",
                "stage": "completed",
                "progress": 100,
            }

            manager._save_preflight(preflight)
            manager._save_job(job)

            state = manager._load_state()
            self.assertNotIn("preflight", state)
            self.assertNotIn("job", state)
            self.assertEqual(preflight, manager._load_preflight())
            self.assertEqual("completed", manager.get_job(job["job_id"])["status"])
            for sidecar in (
                manager._sidecar_path("preflight"),
                manager._sidecar_path("job"),
            ):
                self.assertEqual(0o600, sidecar.stat().st_mode & 0o777)

    def test_legacy_embedded_job_is_compacted_on_first_read(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            manager = InfiniDyskMigrationManager(state_path)
            manager._save_state(
                {
                    "state_version": 2,
                    "preflight": {"inventory": "x" * 1024},
                    "job": {
                        "job_id": "b" * 32,
                        "status": "completed",
                        "stage": "completed",
                        "progress": 100,
                    },
                }
            )

            job = manager.get_job()

            self.assertEqual("completed", job["status"])
            self.assertTrue(manager._sidecar_path("job").is_file())
            self.assertTrue(manager._sidecar_path("preflight").is_file())
            self.assertNotIn("job", manager._load_state())
            self.assertNotIn("preflight", manager._load_state())

    def test_job_sidecar_reads_do_not_load_main_migration_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = InfiniDyskMigrationManager(Path(temp_dir) / "state.json")
            manager._save_job(
                {
                    "job_id": "c" * 32,
                    "status": "completed",
                    "stage": "completed",
                    "progress": 100,
                }
            )

            with patch.object(
                manager,
                "_load_state",
                side_effect=AssertionError("main state should not be parsed"),
            ):
                job = manager.get_job()

            self.assertEqual("completed", job["status"])

    def test_existing_rollback_attention_job_is_enriched_from_private_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = InfiniDyskMigrationManager(Path(temp_dir) / "state.json")
            manager._save_state(
                {
                    "status": "rollback_attention_required",
                    "last_error": "Reference validation failed",
                    "rollback_errors": ["Plex libraries: restore failed"],
                    "backup_bundle_path": "/config/migrations/infinidysk-backups/example",
                    "config_backup_path": "/config/migrations/infinidysk-backups/config.json",
                }
            )
            manager._save_job(
                {
                    "job_id": "d" * 32,
                    "status": "rollback_attention_required",
                    "stage": "rollback_attention_required",
                    "message": "Rollback needs attention.",
                    "progress": 100,
                }
            )

            job = manager.get_job()

            recovery = job["result"]["recovery"]
            self.assertTrue(recovery["manual_restore_required"])
            self.assertEqual(
                ["Plex libraries: restore failed"], recovery["rollback_errors"]
            )
            self.assertEqual(
                "/config/migrations/infinidysk-backups/example",
                recovery["backup_bundle_path"],
            )

    def test_namespace_backup_records_original_file_ownership_and_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "nzbdav"
            config_dir.mkdir()
            database = config_dir / "db.sqlite"
            database.write_text("database", encoding="utf-8")
            database.chmod(0o640)
            backup_path = root / "backups" / "config.json"
            backup_path.parent.mkdir()
            backup_path.write_text("{}", encoding="utf-8")
            manager = InfiniDyskMigrationManager(root / "state.json")
            config_manager = MagicMock()
            config_manager.config = {
                "infinidysk": {"enabled": True, "config_dir": str(config_dir)}
            }

            with (
                patch("utils.infinidysk_migration.CONFIG_MANAGER", config_manager),
                patch.object(manager, "_backup_config", return_value=backup_path),
            ):
                _config_backup, bundle = manager._create_namespace_backup({}, 123)

            manifest = json.loads((bundle / "files.json").read_text(encoding="utf-8"))[
                "files"
            ]
            database_record = next(
                item for item in manifest if item["source"] == str(database)
            )
            database_stat = database.stat()
            self.assertEqual(0o640, database_record["mode"])
            self.assertEqual(database_stat.st_uid, database_record["uid"])
            self.assertEqual(database_stat.st_gid, database_record["gid"])

    def test_namespace_restore_reapplies_original_file_ownership_and_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle = root / "bundle"
            bundle.mkdir()
            backup = bundle / "db.sqlite"
            backup.write_text("restored", encoding="utf-8")
            backup.chmod(0o600)
            destination = root / "nzbdav" / "db.sqlite"
            destination.parent.mkdir()
            destination.write_text("changed", encoding="utf-8")
            InfiniDyskMigrationManager._write_private_json(
                bundle / "files.json",
                {
                    "files": [
                        {
                            "source": str(destination),
                            "backup": str(backup),
                            "size": backup.stat().st_size,
                            "mode": 0o640,
                            "uid": 1234,
                            "gid": 2345,
                        }
                    ]
                },
            )

            with patch("utils.infinidysk_migration.os.chown") as chown:
                errors = InfiniDyskMigrationManager._restore_backup_files(bundle)

            self.assertEqual([], errors)
            self.assertEqual("restored", destination.read_text(encoding="utf-8"))
            self.assertEqual(0o640, destination.stat().st_mode & 0o777)
            chown.assert_called_once_with(destination, 1234, 2345)

    def test_namespace_rollback_restores_and_validates_exact_symlink_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            symlink_root = root / "nzbdav-symlinks"
            symlink_root.mkdir()
            first = symlink_root / "Movie One.mkv"
            second = symlink_root / "Movie Two.mkv"
            first.symlink_to("/mnt/debrid/nzbdav/movies/one.mkv")
            second.symlink_to("../../nzbdav/movies/two.mkv")
            bundle = root / "bundle"
            bundle.mkdir()
            InfiniDyskMigrationManager._write_private_json(
                bundle / "symlinks-before.json",
                {
                    "manifest_type": "symlink_snapshot",
                    "roots": [str(symlink_root)],
                    "entries": [
                        {
                            "link_path": str(first),
                            "target": "/mnt/debrid/nzbdav/movies/one.mkv",
                        },
                        {
                            "link_path": str(second),
                            "target": "../../nzbdav/movies/two.mkv",
                        },
                    ],
                },
            )
            first.unlink()
            first.symlink_to("/mnt/debrid/infinidysk/movies/one.mkv")
            second.unlink()

            errors = InfiniDyskMigrationManager._restore_and_validate_symlink_manifest(
                bundle
            )

            self.assertEqual([], errors)
            self.assertEqual(
                "/mnt/debrid/nzbdav/movies/one.mkv", first.readlink().as_posix()
            )
            self.assertEqual(
                "../../nzbdav/movies/two.mkv", second.readlink().as_posix()
            )

    def test_namespace_symlink_restore_refuses_to_create_missing_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing_root = root / "missing-nzbdav-symlinks"
            link = missing_root / "Movie.mkv"
            bundle = root / "bundle"
            bundle.mkdir()
            InfiniDyskMigrationManager._write_private_json(
                bundle / "symlinks-before.json",
                {
                    "manifest_type": "symlink_snapshot",
                    "roots": [str(missing_root)],
                    "entries": [
                        {
                            "link_path": str(link),
                            "target": "/mnt/debrid/nzbdav/movies/movie.mkv",
                        }
                    ],
                },
            )

            errors = InfiniDyskMigrationManager._restore_and_validate_symlink_manifest(
                bundle
            )

            self.assertEqual(1, len(errors))
            self.assertIn("refusing to create a split symlink tree", errors[0])
            self.assertFalse(missing_root.exists())

    def test_namespace_symlink_restore_reports_missing_expected_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = Path(temp_dir) / "bundle"
            bundle.mkdir()
            InfiniDyskMigrationManager._write_private_json(
                bundle / "preflight.json",
                {
                    "filesystem": [
                        {
                            "source": "/mnt/debrid/nzbdav-symlinks",
                            "destination": "/mnt/debrid/infinidysk-symlinks",
                        }
                    ]
                },
            )

            errors = InfiniDyskMigrationManager._restore_and_validate_symlink_manifest(
                bundle
            )

            self.assertEqual(["captured symlink manifest is missing or unsafe"], errors)

    def test_arr_item_updates_use_bounded_bulk_editor_batches(self):
        total = ARR_EDITOR_BATCH_SIZE + 1
        snapshot = {
            "service_key": "radarr",
            "host": "http://127.0.0.1:7878",
            "api_key": "secret",
            "api_version": "v3",
            "item_endpoint": "movie",
            "roots": [{"id": 1, "path": "/mnt/debrid/nzbdav-symlinks/movies"}],
            "items": [
                {
                    "id": item_id,
                    "path": f"/mnt/debrid/nzbdav-symlinks/movies/Movie {item_id}",
                }
                for item_id in range(1, total + 1)
            ],
        }
        desired = InfiniDyskMigrationManager._desired_arr_snapshot(snapshot)
        requests = []
        progress = []

        def request(url, _token, method="GET", data=None):
            requests.append((url, method, data))

        with patch(
            "utils.infinidysk_migration._migration_arr_req", side_effect=request
        ):
            changed = InfiniDyskMigrationManager._update_arr_items(
                snapshot,
                desired,
                progress_callback=lambda completed, expected, mode: progress.append(
                    (completed, expected, mode)
                ),
            )

        self.assertEqual(total, changed)
        self.assertEqual(2, len(requests))
        self.assertTrue(all("movie/editor" in request[0] for request in requests))
        self.assertEqual(ARR_EDITOR_BATCH_SIZE, len(requests[0][2]["movieIds"]))
        self.assertEqual((total, total, "bulk"), progress[-1])

    def test_arr_bulk_conflict_is_bisected_without_serializing_whole_batch(self):
        total = ARR_EDITOR_BATCH_SIZE
        conflicting_id = 377
        snapshot = {
            "service_key": "radarr",
            "host": "http://127.0.0.1:7878",
            "api_key": "secret",
            "api_version": "v3",
            "item_endpoint": "movie",
            "roots": [{"id": 1, "path": "/mnt/debrid/nzbdav-symlinks/movies"}],
            "items": [
                {
                    "id": item_id,
                    "path": f"/mnt/debrid/nzbdav-symlinks/movies/Movie {item_id}",
                }
                for item_id in range(1, total + 1)
            ],
        }
        desired = InfiniDyskMigrationManager._desired_arr_snapshot(snapshot)
        bulk_requests = []
        individual_requests = []

        def request(url, _token, method="GET", data=None):
            if "movie/editor" in url:
                ids = data["movieIds"]
                bulk_requests.append(ids)
                if conflicting_id in ids:
                    raise urllib.error.HTTPError(url, 409, "Conflict", {}, None)
                return None
            individual_requests.append(url)
            return None

        with patch(
            "utils.infinidysk_migration._migration_arr_req", side_effect=request
        ):
            changed = InfiniDyskMigrationManager._update_arr_items(snapshot, desired)

        self.assertEqual(total, changed)
        self.assertLess(len(bulk_requests), 20)
        self.assertLessEqual(len(individual_requests), 8)
        self.assertTrue(
            any(
                url.endswith(f"movie/{conflicting_id}?moveFiles=false")
                for url in individual_requests
            )
        )

    def test_arr_unsupported_bulk_editor_falls_back_without_recursive_probes(self):
        total = 20
        snapshot = {
            "service_key": "radarr",
            "host": "http://127.0.0.1:7878",
            "api_key": "secret",
            "api_version": "v3",
            "item_endpoint": "movie",
            "roots": [{"id": 1, "path": "/mnt/debrid/nzbdav-symlinks/movies"}],
            "items": [
                {
                    "id": item_id,
                    "path": f"/mnt/debrid/nzbdav-symlinks/movies/Movie {item_id}",
                }
                for item_id in range(1, total + 1)
            ],
        }
        desired = InfiniDyskMigrationManager._desired_arr_snapshot(snapshot)
        bulk_requests = 0
        individual_requests = 0

        def request(url, _token, method="GET", data=None):
            nonlocal bulk_requests, individual_requests
            if "movie/editor" in url:
                bulk_requests += 1
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            individual_requests += 1
            return None

        with patch(
            "utils.infinidysk_migration._migration_arr_req", side_effect=request
        ):
            changed = InfiniDyskMigrationManager._update_arr_items(snapshot, desired)

        self.assertEqual(total, changed)
        self.assertEqual(1, bulk_requests)
        self.assertEqual(total, individual_requests)

    def test_rolled_back_job_is_terminal_at_100_percent_with_cause(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = InfiniDyskMigrationManager(Path(temp_dir) / "state.json")
            config = {"infinidysk": {"enabled": True}}
            token = "preflight-token"
            manager._save_state(
                {
                    "preflight": {
                        "token": token,
                        "expires_at": int(time.time()) + 300,
                        "blockers": [],
                        "config_fingerprint": _config_fingerprint(config),
                    }
                }
            )
            updater = MagicMock()
            updater.updating = threading.Lock()

            def failed_job(*_args, progress_callback=None, **_kwargs):
                progress_callback(
                    "rollback_services",
                    "Restarting the original services.",
                    84,
                    "rolling_back",
                )
                state = manager._load_state()
                state.update(
                    {
                        "status": "failed_rolled_back",
                        "last_error": "InfiniDysk failed to start",
                        "rollback_errors": [],
                        "backup_bundle_path": "/config/migrations/infinidysk-backups/example",
                        "config_backup_path": "/config/migrations/infinidysk-backups/config.json",
                    }
                )
                manager._save_state(state)
                raise InfiniDyskMigrationError(
                    "The full namespace migration failed and was rolled back. "
                    "Cause: InfiniDysk failed to start"
                )

            config_manager = MagicMock()
            config_manager.config = config
            with (
                patch("utils.infinidysk_migration.CONFIG_MANAGER", config_manager),
                patch.object(manager, "apply_full_namespace", side_effect=failed_job),
            ):
                started = manager.start_full_namespace_job(
                    token, True, MagicMock(), MagicMock(), updater
                )
                manager._active_job_thread.join(timeout=3)

            persisted = manager.get_job(started["job_id"])
            self.assertEqual("failed_rolled_back", persisted["status"])
            self.assertEqual(100, persisted["progress"])
            self.assertIn("InfiniDysk failed to start", persisted["message"])
            recovery = persisted["result"]["recovery"]
            self.assertFalse(recovery["manual_restore_required"])
            self.assertEqual([], recovery["rollback_errors"])
            self.assertEqual(
                "/config/migrations/infinidysk-backups/example",
                recovery["backup_bundle_path"],
            )

    def test_active_job_from_previous_process_is_marked_interrupted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            manager = InfiniDyskMigrationManager(state_path)
            manager._save_state(
                {
                    "job": {
                        "job_id": "a" * 32,
                        "status": "running",
                        "stage": "filesystem",
                        "message": "Moving paths.",
                        "progress": 38,
                        "events": [],
                        "worker_id": "previous-process-worker",
                    }
                }
            )

            restored = InfiniDyskMigrationManager(state_path).get_job()

            self.assertEqual("interrupted", restored["status"])
            self.assertEqual(38, restored["progress"])
            self.assertIn("restarted", restored["message"])

    def test_partial_stop_failure_restarts_services_already_stopped(self):
        states = {"Provider": True, "Consumer": True}

        class Process:
            def __init__(self, name):
                self.name = name

            def poll(self):
                return None if states[self.name] else 0

        class Handler:
            process_names = {
                "Provider": Process("Provider"),
                "Consumer": Process("Consumer"),
            }

            @staticmethod
            def _prefixed_name(value):
                return value

            @staticmethod
            def stop_process(name):
                if name == "Consumer":
                    states[name] = False

            @staticmethod
            def start_process(name):
                states[name] = True
                return True, None

        with self.assertRaisesRegex(RuntimeError, "Provider did not stop cleanly"):
            InfiniDyskMigrationManager._stop_processes(
                Handler(), ["Provider", "Consumer"]
            )

        self.assertTrue(states["Provider"])
        self.assertTrue(states["Consumer"])

    def test_path_move_refuses_destination_created_after_preflight(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_source = root / "nzbdav-first.log"
            first_destination = root / "infinidysk-first.log"
            source = root / "nzbdav.log"
            destination = root / "infinidysk.log"
            first_source.write_text("first", encoding="utf-8")
            source.write_text("legacy", encoding="utf-8")
            destination.symlink_to(source)

            with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                InfiniDyskMigrationManager._move_namespace_paths(
                    [
                        {
                            "source": str(first_source),
                            "destination": str(first_destination),
                            "destination_state": "absent",
                        },
                        {
                            "source": str(source),
                            "destination": str(destination),
                            "destination_state": "absent",
                        },
                    ]
                )

            self.assertEqual("first", first_source.read_text(encoding="utf-8"))
            self.assertFalse(first_destination.exists())
            self.assertEqual("legacy", source.read_text(encoding="utf-8"))
            self.assertTrue(destination.is_symlink())

    def test_namespace_plan_preserves_nested_renames_after_parent_move(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_parent = root / "nzbdav-symlinks"
            destination_parent = root / "infinidysk-symlinks"
            source_child = source_parent / "radarr-nzbdav"
            destination_child = destination_parent / "radarr-infinidysk"
            source_child.mkdir(parents=True)
            (source_child / "marker").write_text("content", encoding="utf-8")
            config = {
                "infinidysk": {
                    "config_dir": str(root / "unrelated"),
                    "symlink_backup_roots": [str(source_parent)],
                },
                "radarr": {
                    "instances": {
                        "NzbDAV": {
                            "root_folder": str(source_child),
                        }
                    }
                },
            }

            with patch(
                "utils.infinidysk_migration.NAMESPACE_PATH_MAPPINGS",
                ((str(source_parent), str(destination_parent)),),
            ):
                actions, blockers = (
                    InfiniDyskMigrationManager._namespace_filesystem_plan(config)
                )

            self.assertEqual([], blockers)
            self.assertEqual(
                [
                    (str(source_parent), str(destination_parent)),
                    (
                        str(destination_parent / "radarr-nzbdav"),
                        str(destination_child),
                    ),
                ],
                [(action["source"], action["destination"]) for action in actions],
            )

            moved = InfiniDyskMigrationManager._move_namespace_paths(actions)
            InfiniDyskMigrationManager._validate_namespace_paths(moved)
            self.assertFalse(source_parent.exists())
            self.assertFalse((destination_parent / "radarr-nzbdav").exists())
            self.assertEqual(
                "content",
                (destination_child / "marker").read_text(encoding="utf-8"),
            )

            self.assertEqual(
                [], InfiniDyskMigrationManager._rollback_namespace_paths(moved)
            )
            self.assertEqual(
                "content", (source_child / "marker").read_text(encoding="utf-8")
            )
            self.assertFalse(destination_parent.exists())

    def test_database_migration_rewrites_virtual_category_records(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            database = config_dir / "db.sqlite"
            with sqlite3.connect(database) as connection:
                connection.executescript("""
                    CREATE TABLE ConfigItems (
                        ConfigName TEXT PRIMARY KEY,
                        ConfigValue TEXT
                    );
                    CREATE TABLE HistoryItems (
                        Id INTEGER PRIMARY KEY,
                        Category TEXT
                    );
                    CREATE TABLE QueueItems (
                        Id INTEGER PRIMARY KEY,
                        Category TEXT
                    );
                    CREATE TABLE DavItems (
                        Id INTEGER PRIMARY KEY,
                        Name TEXT,
                        Path TEXT
                    );
                    """)
                connection.execute(
                    "INSERT INTO ConfigItems VALUES (?, ?)",
                    ("category", "radarr-nzbdav"),
                )
                connection.executemany(
                    "INSERT INTO HistoryItems VALUES (?, ?)",
                    [(1, "radarr-nzbdav"), (2, "custom-category")],
                )
                connection.execute(
                    "INSERT INTO QueueItems VALUES (?, ?)",
                    (1, "sonarr-nzbdav"),
                )
                connection.execute(
                    "INSERT INTO DavItems VALUES (?, ?, ?)",
                    (
                        1,
                        "nzbdav-movies",
                        "/content/radarr-nzbdav/Example Movie",
                    ),
                )

            changed = InfiniDyskMigrationManager._migrate_infinidysk_database(
                str(config_dir)
            )

            self.assertEqual(5, changed)
            self.assertEqual(
                0,
                InfiniDyskMigrationManager._migrate_infinidysk_database(
                    str(config_dir)
                ),
            )
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    "radarr-infinidysk",
                    connection.execute(
                        "SELECT ConfigValue FROM ConfigItems WHERE ConfigName='category'"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    [(1, "radarr-infinidysk"), (2, "custom-category")],
                    connection.execute(
                        "SELECT Id, Category FROM HistoryItems ORDER BY Id"
                    ).fetchall(),
                )
                self.assertEqual(
                    "sonarr-infinidysk",
                    connection.execute(
                        "SELECT Category FROM QueueItems WHERE Id=1"
                    ).fetchone()[0],
                )
                self.assertEqual(
                    ("infinidysk-movies", "/content/radarr-infinidysk/Example Movie"),
                    connection.execute(
                        "SELECT Name, Path FROM DavItems WHERE Id=1"
                    ).fetchone(),
                )

    def test_namespace_validation_rejects_legacy_raw_symlink_targets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            link = root / "example"
            link.symlink_to("/mnt/debrid/nzbdav/content/example")

            with self.assertRaisesRegex(RuntimeError, "1 legacy target"):
                InfiniDyskMigrationManager._validate_symlink_targets([str(root)])

            link.unlink()
            link.symlink_to("/mnt/debrid/infinidysk/content/example")
            InfiniDyskMigrationManager._validate_symlink_targets([str(root)])

    def test_arr_and_media_filesystem_validation_requires_migrated_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_root = root / "nzbdav-symlinks" / "radarr-nzbdav"
            new_root = root / "infinidysk-symlinks" / "radarr-infinidysk"
            old_item = old_root / "Example"
            new_item = new_root / "Example"
            new_root.mkdir(parents=True)
            arr_snapshot = {
                "roots": [{"id": 1, "path": str(old_root)}],
                "items": [{"id": 2, "path": str(old_item), "hasFile": True}],
            }
            arr_desired = {
                "roots": [{"id": 1, "path": str(new_root)}],
                "items": [{"id": 2, "path": str(new_item), "hasFile": True}],
            }

            with self.assertRaisesRegex(RuntimeError, "1 file-bearing item"):
                InfiniDyskMigrationManager._validate_arr_filesystem(
                    arr_snapshot, arr_desired
                )
            new_item.mkdir()
            InfiniDyskMigrationManager._validate_arr_filesystem(
                arr_snapshot, arr_desired
            )

            missing_library = root / "infinidysk-symlinks" / "missing"
            media_snapshot = {"libraries": [{"id": 3, "paths": [str(old_root)]}]}
            media_desired = {"libraries": [{"id": 3, "paths": [str(missing_library)]}]}
            with self.assertRaisesRegex(RuntimeError, "media-library paths"):
                InfiniDyskMigrationManager._validate_media_filesystem(
                    media_snapshot, media_desired
                )
            missing_library.mkdir()
            InfiniDyskMigrationManager._validate_media_filesystem(
                media_snapshot, media_desired
            )

    def test_namespace_rewrite_is_prefix_scoped_and_updates_managed_categories(self):
        self.assertEqual(
            "/mnt/debrid/infinidysk-symlinks/movies",
            _replace_namespace_text("/mnt/debrid/nzbdav-symlinks/movies"),
        )
        self.assertEqual(
            "https://example.invalid/nzbdav",
            _replace_namespace_text("https://example.invalid/nzbdav"),
        )
        self.assertEqual(
            "/log/rclone_w_infinidysk.log",
            _replace_namespace_text("/log/rclone_w_nzbdav.log"),
        )
        rewritten = _rewrite_config_namespace(
            {
                "process_name": "Radarr NzbDAV",
                "mount_name": "nzbdav",
                "category": "nzbdav-movies",
                "path": "/mnt/debrid/nzbdav/library",
            }
        )
        self.assertEqual("Radarr NzbDAV", rewritten["process_name"])
        self.assertEqual("infinidysk", rewritten["mount_name"])
        self.assertEqual("infinidysk-movies", rewritten["category"])
        self.assertEqual("/mnt/debrid/infinidysk/library", rewritten["path"])

    def test_namespace_rewrite_updates_singular_plural_and_combined_core_links(self):
        rewritten = _rewrite_config_namespace(
            {
                "singular": {"core_service": "decypharr, nzbdav"},
                "plural": {"core_services": ["nzbdav", "altmount, nzbdav"]},
                "rclone": {"key_type": "nzbdav"},
            }
        )

        self.assertEqual("decypharr,infinidysk", rewritten["singular"]["core_service"])
        self.assertEqual(
            ["infinidysk", "altmount,infinidysk"],
            rewritten["plural"]["core_services"],
        )
        self.assertEqual("infinidysk", rewritten["rclone"]["key_type"])

    def test_all_valid_core_consumers_are_affected_but_zurg_is_not(self):
        config = {
            "infinidysk": {"process_name": "NzbDAV"},
            "rclone": {
                "instances": {
                    "Usenet": {
                        "enabled": True,
                        "process_name": "Rclone w/ NzbDAV",
                        "key_type": "nzbdav",
                    }
                }
            },
            "radarr": {
                "instances": {
                    "Usenet": {
                        "enabled": True,
                        "process_name": "Radarr NzbDAV",
                        "core_service": "nzbdav",
                    }
                }
            },
            "sonarr": {
                "instances": {
                    "Combined": {
                        "enabled": True,
                        "process_name": "Sonarr Combined",
                        "core_services": ["decypharr, nzbdav"],
                    }
                }
            },
            "lidarr": {
                "instances": {
                    "Usenet": {
                        "enabled": True,
                        "process_name": "Lidarr Usenet",
                        "core_service": "infinidysk",
                    }
                }
            },
            "whisparr": {
                "instances": {
                    "Usenet": {
                        "enabled": True,
                        "process_name": "Whisparr Usenet",
                        "core_service": "infinidysk",
                    }
                }
            },
            "neutarr": {
                "instances": {
                    "Usenet": {
                        "enabled": True,
                        "process_name": "NeutArr NzbDAV",
                        "core_service": "nzbdav",
                    }
                }
            },
            "profilarr": {
                "instances": {
                    "Usenet": {
                        "enabled": True,
                        "process_name": "Profilarr NzbDAV",
                        "core_service": ["nzbdav", "decypharr"],
                    }
                }
            },
            "seerr": {
                "instances": {
                    "Usenet": {
                        "enabled": True,
                        "process_name": "Seerr NzbDAV",
                        "core_service": "nzbdav",
                    }
                }
            },
            "zurg": {
                "instances": {
                    "InvalidLegacyLink": {
                        "enabled": True,
                        "process_name": "Zurg Usenet",
                        "core_service": "nzbdav",
                    }
                }
            },
            "prowlarr": {
                "instances": {
                    "NamedOnly": {
                        "enabled": True,
                        "process_name": "Prowlarr NzbDAV",
                    }
                }
            },
        }

        linked = InfiniDyskMigrationManager._linked_service_inventory(config, None)
        linked_keys = {item["service_key"] for item in linked}
        self.assertEqual(
            {
                "rclone",
                "radarr",
                "sonarr",
                "lidarr",
                "whisparr",
                "neutarr",
                "profilarr",
                "seerr",
            },
            linked_keys,
        )
        self.assertNotIn("zurg", linked_keys)
        self.assertNotIn("prowlarr", linked_keys)

        attached_keys = {
            item["service_key"]
            for item in InfiniDyskMigrationManager._attached_services(config)
        }
        self.assertIn("profilarr", attached_keys)
        self.assertIn("prowlarr", attached_keys)
        self.assertNotIn("zurg", attached_keys)

        affected = InfiniDyskMigrationManager._affected_processes(config, {})
        self.assertIn("Profilarr NzbDAV", affected)
        self.assertIn("Prowlarr NzbDAV", affected)
        self.assertNotIn("Zurg Usenet", affected)
        affected_without_renames = InfiniDyskMigrationManager._affected_processes(
            config, {}, rename_attached_services=False
        )
        self.assertIn("Profilarr NzbDAV", affected_without_renames)
        self.assertNotIn("Prowlarr NzbDAV", affected_without_renames)

        renamed = InfiniDyskMigrationManager._renamed_processes(
            affected, "NzbDAV", True
        )
        self.assertEqual("Profilarr InfiniDysk", renamed["Profilarr NzbDAV"])
        self.assertEqual("Prowlarr InfiniDysk", renamed["Prowlarr NzbDAV"])

    def test_linked_and_renamed_running_services_rerun_setup_under_new_names(self):
        handler = MagicMock()
        handler.setup_tracker = {
            "InfiniDysk",
            "Rclone w/ InfiniDysk",
            "NeutArr InfiniDysk",
            "Profilarr InfiniDysk",
            "Seerr InfiniDysk",
        }
        handler.setup_tracker_lock = threading.Lock()
        handler.start_process.return_value = (True, None)
        process_names = [
            "InfiniDysk",
            "Rclone w/ InfiniDysk",
            "NeutArr InfiniDysk",
            "Profilarr InfiniDysk",
            "Seerr InfiniDysk",
        ]

        InfiniDyskMigrationManager._start_processes(handler, process_names)

        self.assertEqual(set(), handler.setup_tracker)
        self.assertEqual(
            [call(name) for name in process_names],
            handler.start_process.call_args_list,
        )

    def test_provider_restart_defers_live_arr_setup_until_consumers_are_running(self):
        handler = MagicMock()
        handler.setup_tracker = {"InfiniDysk", "Radarr InfiniDysk"}
        handler.setup_tracker_lock = threading.Lock()
        handler.start_process.return_value = (True, None)
        context = MagicMock()
        context.__enter__.return_value = None
        context.__exit__.return_value = False

        with patch(
            "utils.infinidysk_migration.defer_nzbdav_runtime_integrations",
            return_value=context,
        ) as defer:
            InfiniDyskMigrationManager._start_processes(
                handler,
                ["InfiniDysk", "Radarr InfiniDysk"],
                defer_provider_integrations=True,
            )

        defer.assert_called_once_with()
        context.__enter__.assert_called_once_with()
        context.__exit__.assert_called_once()
        self.assertEqual(
            [call("InfiniDysk"), call("Radarr InfiniDysk")],
            handler.start_process.call_args_list,
        )

    def test_legacy_core_reference_validation_excludes_zurg(self):
        config = {
            "seerr": {
                "instances": {"Default": {"core_services": ["decypharr", "nzbdav"]}}
            },
            "rclone": {"instances": {"Usenet": {"key_type": "nzbdav"}}},
            "zurg": {"instances": {"Unused": {"core_service": "nzbdav"}}},
        }

        references = InfiniDyskMigrationManager._legacy_core_service_references(config)

        self.assertEqual(
            {
                ("seerr", "Default", "core_services"),
                ("rclone", "Usenet", "key_type"),
            },
            {
                (item["service_key"], item["instance_name"], item["field"])
                for item in references
            },
        )

    def test_legacy_path_inventory_excludes_zurg(self):
        config = {
            "infinidysk": {"config_dir": "/nzbdav"},
            "zurg": {
                "instances": {
                    "Unused": {
                        "core_service": "nzbdav",
                        "config_dir": "/data/nzbdav-zurg-placeholder",
                    }
                }
            },
        }

        paths = InfiniDyskMigrationManager._legacy_paths(config)

        self.assertEqual(
            [{"config_path": "infinidysk.config_dir", "value": "/nzbdav"}],
            paths,
        )

    def test_real_config_manager_cutover_changes_identity_but_not_paths(self):
        from tests.test_env_example import _load_config_manager_class

        with tempfile.TemporaryDirectory() as temp_dir:
            config_manager_class = _load_config_manager_class()
            config = json.loads((ROOT / "utils" / "dumb_config.json").read_text())
            config["nzbdav"] = config.pop("infinidysk")
            config["nzbdav"].update(
                {
                    "enabled": True,
                    "process_name": "NzbDAV",
                    "repo_owner": "nzbdav",
                    "repo_name": "nzbdav",
                    "config_dir": "/nzbdav",
                    "log_file": "/log/nzbdav.log",
                    "symlink_backup_roots": ["/mnt/debrid/nzbdav-symlinks"],
                }
            )
            config["radarr"]["instances"]["Default"].update(
                {
                    "core_service": "nzbdav",
                    "process_name": "Radarr NzbDAV",
                }
            )
            config_path = Path(temp_dir) / "dumb_config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            config_manager = config_manager_class(
                file_path=str(config_path),
                schema_path=str(ROOT / "utils" / "dumb_config_schema.json"),
            )
            migration = InfiniDyskMigrationManager(Path(temp_dir) / "state.json")

            with patch("utils.infinidysk_migration.CONFIG_MANAGER", config_manager):
                result = migration.apply_brand_cutover(rename_attached_services=True)

            persisted = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertNotIn("nzbdav", persisted)
            self.assertEqual("InfiniDysk", persisted["infinidysk"]["process_name"])
            self.assertEqual("/nzbdav", persisted["infinidysk"]["config_dir"])
            self.assertEqual("/log/nzbdav.log", persisted["infinidysk"]["log_file"])
            self.assertEqual(
                "infinidysk",
                persisted["radarr"]["instances"]["Default"]["core_service"],
            )
            self.assertEqual(
                "Radarr InfiniDysk",
                persisted["radarr"]["instances"]["Default"]["process_name"],
            )
            backup = json.loads(
                Path(result["config_backup_path"]).read_text(encoding="utf-8")
            )
            self.assertIn("nzbdav", backup)
            self.assertNotIn("infinidysk", backup)

    def test_status_reports_due_notice_and_exact_legacy_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = InfiniDyskMigrationManager(Path(temp_dir) / "state.json")
            status = manager.status(legacy_config(), now=1_000)

        self.assertTrue(status["eligible"])
        self.assertTrue(status["notice_due"])
        self.assertTrue(status["legacy"]["repository"])
        self.assertEqual(
            "Radarr InfiniDysk",
            status["legacy"]["attached_services"][0]["suggested_process_name"],
        )
        self.assertTrue(
            any(
                item["value"] == "/mnt/debrid/nzbdav-symlinks"
                for item in status["legacy"]["paths"]
            )
        )

    def test_remind_later_is_persisted_server_side(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = InfiniDyskMigrationManager(Path(temp_dir) / "state.json")
            with patch("utils.infinidysk_migration.CONFIG_MANAGER") as config_manager:
                config_manager.config = legacy_config()
                status = manager.remind_later(days=14, now=1_000)

            self.assertFalse(status["notice_due"])
            self.assertEqual(1_000 + (14 * 86400), status["snoozed_until"])

    def test_completed_brand_cutover_retains_legacy_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = InfiniDyskMigrationManager(Path(temp_dir) / "state.json")
            config = legacy_config()
            with (
                patch("utils.infinidysk_migration.CONFIG_MANAGER") as config_manager,
                patch.object(
                    manager, "_backup_config", wraps=manager._backup_config
                ) as backup_config,
            ):
                config_manager.config = config
                config_manager.file_path = Path(temp_dir) / "dumb_config.json"
                config_manager.file_path.write_text(
                    json.dumps({"nzbdav": config["infinidysk"]}), encoding="utf-8"
                )
                config_manager.uses_legacy_infinidysk_identity.return_value = True
                result = manager.apply_brand_cutover(rename_attached_services=True)
                config_manager.uses_legacy_infinidysk_identity.return_value = False
                repeated = manager.apply_brand_cutover(rename_attached_services=True)

            self.assertEqual("completed", result["status"])
            self.assertEqual("infinidysk", config["infinidysk"]["repo_owner"])
            self.assertEqual("InfiniDysk", config["infinidysk"]["process_name"])
            self.assertEqual("/nzbdav", config["infinidysk"]["config_dir"])
            self.assertEqual(
                "Radarr InfiniDysk",
                config["radarr"]["instances"]["InfiniDysk"]["process_name"],
            )
            sidebar = config["dumb"]["ui"]["sidebar"]
            self.assertEqual(
                ["InfiniDysk", "Radarr InfiniDysk", "Plex"],
                sidebar["service_order"],
            )
            self.assertEqual("InfiniDysk", sidebar["service_shortcuts"]["alt+i"])
            self.assertEqual("Radarr InfiniDysk", sidebar["service_shortcuts"]["alt+r"])
            self.assertEqual(
                ["InfiniDysk", "Radarr InfiniDysk", "Plex"],
                config["dumb"]["notifications"]["destinations"][0]["service_names"],
            )
            self.assertEqual(6, result["changed_references"])
            config_manager.save_config.assert_called_once_with()
            config_manager.adopt_infinidysk_identity.assert_called_once_with()
            backup_path = Path(result["config_backup_path"])
            self.assertTrue(backup_path.is_file())
            self.assertIn("nzbdav", json.loads(backup_path.read_text(encoding="utf-8")))
            self.assertEqual("completed", repeated["status"])
            self.assertFalse(repeated["restart_required"])
            self.assertEqual(
                result["config_backup_path"], repeated["config_backup_path"]
            )
            self.assertEqual(1, backup_config.call_count)
            self.assertEqual(1, config_manager.save_config.call_count)

            status = manager.status(config, now=2_000)
            self.assertEqual("compatibility_completed", status["status"])
            self.assertTrue(status["notice_due"])
            with patch("utils.infinidysk_migration.CONFIG_MANAGER") as config_manager:
                config_manager.config = config
                config_manager.uses_legacy_infinidysk_identity.return_value = False
                snoozed = manager.remind_later(days=7, now=2_000)
            self.assertEqual("compatibility_completed", snoozed["status"])
            self.assertFalse(snoozed["notice_due"])

    def test_restoring_legacy_identity_makes_a_completed_notice_due_again(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = InfiniDyskMigrationManager(Path(temp_dir) / "state.json")
            config = legacy_config()
            with patch("utils.infinidysk_migration.CONFIG_MANAGER") as config_manager:
                config_manager.config = config
                config_manager.file_path = Path(temp_dir) / "dumb_config.json"
                config_manager.file_path.write_text(
                    json.dumps({"nzbdav": config["infinidysk"]}), encoding="utf-8"
                )
                config_manager.uses_legacy_infinidysk_identity.return_value = False
                manager.apply_brand_cutover(rename_attached_services=False)
                config["infinidysk"]["repo_owner"] = "nzbdav"
                config["infinidysk"]["repo_name"] = "nzbdav"
                config["infinidysk"]["process_name"] = "NzbDAV"
                status = manager.status(config, now=2_000)

        self.assertEqual("pending", status["status"])
        self.assertTrue(status["notice_due"])

    def test_fresh_canonical_install_cannot_be_forced_through_migration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = InfiniDyskMigrationManager(Path(temp_dir) / "state.json")
            config = {
                "infinidysk": {
                    "enabled": False,
                    "process_name": "InfiniDysk",
                    "repo_owner": "infinidysk",
                    "repo_name": "infinidysk",
                    "config_dir": "/infinidysk",
                    "symlink_backup_roots": ["/mnt/debrid/infinidysk-symlinks"],
                }
            }
            with patch("utils.infinidysk_migration.CONFIG_MANAGER") as manager_config:
                manager_config.config = config
                manager_config.uses_legacy_infinidysk_identity.return_value = False
                with self.assertRaisesRegex(RuntimeError, "No legacy NzbDAV"):
                    manager.apply_brand_cutover()

    def test_full_namespace_preflight_and_apply_move_paths_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "nzbdav"
            destination = root / "infinidysk"
            source.mkdir()
            (source / "db.sqlite").write_bytes(b"")
            attached_source = root / "rclone_w_nzbdav.log"
            attached_destination = root / "rclone_w_infinidysk.log"
            attached_source.write_text("legacy", encoding="utf-8")
            config_path = root / "dumb_config.json"
            config = {
                "infinidysk": {
                    "enabled": False,
                    "process_name": "NzbDAV",
                    "repo_owner": "nzbdav",
                    "repo_name": "nzbdav",
                    "config_dir": str(source),
                    "log_file": "/log/other.log",
                    "symlink_backup_roots": [],
                },
                "rclone": {
                    "instances": {
                        "NzbDAV": {
                            "enabled": False,
                            "process_name": "Rclone w/ NzbDAV",
                            "log_file": str(attached_source),
                        }
                    }
                },
                "zurg": {
                    "instances": {
                        "Unused": {
                            "enabled": False,
                            "process_name": "Zurg Usenet",
                            "core_service": "nzbdav",
                            "config_dir": "/data/nzbdav-zurg-placeholder",
                        }
                    }
                },
            }
            original_zurg = copy_json(config["zurg"])
            config_path.write_text(json.dumps({"nzbdav": config["infinidysk"]}))
            manager = InfiniDyskMigrationManager(root / "state.json")
            manager._save_state(
                {
                    "status": "failed_rolled_back",
                    "failed_at": 1,
                    "last_error": "stale failure from an earlier attempt",
                    "rollback_errors": ["stale rollback detail"],
                }
            )
            handler = MagicMock()
            handler.process_names = {}
            handler._prefixed_name.side_effect = lambda value: value
            manager_config = MagicMock()
            manager_config.config = config
            manager_config.file_path = config_path
            manager_config.uses_legacy_infinidysk_identity.return_value = True
            mapping = ((str(source), str(destination)),)
            with (
                patch("utils.infinidysk_migration.CONFIG_MANAGER", manager_config),
                patch("utils.infinidysk_migration.NAMESPACE_PATH_MAPPINGS", mapping),
                patch.object(manager, "_arr_snapshot", return_value=([], [], [])),
                patch.object(manager, "_media_snapshot", return_value=([], [])),
                patch.object(
                    manager, "_infinidysk_active_reads", return_value=(0, None)
                ),
            ):
                preflight = manager.preflight(handler, MagicMock())
                result = manager.apply_full_namespace(
                    preflight["token"], True, handler, MagicMock()
                )

            self.assertTrue(preflight["ready"])
            self.assertEqual("completed", result["status"])
            self.assertFalse(source.exists())
            self.assertTrue(destination.is_dir())
            self.assertFalse(attached_source.exists())
            self.assertEqual("legacy", attached_destination.read_text(encoding="utf-8"))
            self.assertEqual(
                str(destination), manager_config.config["infinidysk"]["config_dir"]
            )
            self.assertEqual(
                str(attached_destination),
                manager_config.config["rclone"]["instances"]["InfiniDysk"]["log_file"],
            )
            self.assertEqual(original_zurg, manager_config.config["zurg"])
            manager_config.adopt_infinidysk_identity.assert_called_once_with()
            health_guard = json.loads(
                (
                    Path(result["backup_bundle_path"])
                    / "infinidysk-health-check-guard.json"
                ).read_text(encoding="utf-8")
            )
            self.assertFalse(health_guard["applicable"])
            self.assertTrue(health_guard["restored"])
            completed_state = json.loads((root / "state.json").read_text())
            self.assertEqual("completed", completed_state["status"])
            self.assertNotIn("failed_at", completed_state)
            self.assertNotIn("last_error", completed_state)
            self.assertNotIn("rollback_errors", completed_state)

    def test_arr_paths_clients_and_roots_round_trip_through_full_migration(self):
        snapshot = {
            "service_key": "radarr",
            "host": "http://127.0.0.1:7878",
            "api_key": "secret",
            "api_version": "v3",
            "item_endpoint": "movie",
            "roots": [{"id": 1, "path": "/mnt/debrid/nzbdav-symlinks/movies"}],
            "items": [
                {
                    "id": 10,
                    "title": "Example",
                    "path": "/mnt/debrid/nzbdav-symlinks/movies/Example",
                }
            ],
            "clients": [
                {
                    "id": 2,
                    "name": "NzbDAV",
                    "fields": [{"name": "category", "value": "nzbdav-movies"}],
                }
            ],
            "import_lists": [
                {
                    "id": 4,
                    "name": "NzbDAV Watchlist",
                    "rootFolderPath": "/mnt/debrid/nzbdav-symlinks/movies",
                    "fields": [{"name": "accessToken", "value": "secret"}],
                }
            ],
            "collections": [
                {
                    "id": 5,
                    "title": "NzbDAV Collection",
                    "rootFolderPath": "/mnt/debrid/nzbdav-symlinks/movies",
                }
            ],
            "tags": [{"id": 3, "label": "NzbDAV"}],
        }
        state = {
            "roots": copy_json(snapshot["roots"]),
            "items": copy_json(snapshot["items"]),
            "clients": copy_json(snapshot["clients"]),
            "import_lists": copy_json(snapshot["import_lists"]),
            "collections": copy_json(snapshot["collections"]),
            "tags": copy_json(snapshot["tags"]),
            "next_root_id": 20,
        }

        def fake_request(url, _token, method="GET", data=None, **_kwargs):
            endpoint = url.split("/api/v3/", 1)[1]
            if endpoint == "rootfolder" and method == "GET":
                return copy_json(state["roots"])
            if endpoint == "rootfolder" and method == "POST":
                state["roots"].append(
                    {"id": state["next_root_id"], "path": data["path"]}
                )
                state["next_root_id"] += 1
                return state["roots"][-1]
            if endpoint.startswith("rootfolder/") and method == "DELETE":
                root_id = int(endpoint.rsplit("/", 1)[1])
                state["roots"] = [
                    root for root in state["roots"] if root["id"] != root_id
                ]
                return None
            if endpoint == "movie" and method == "GET":
                return copy_json(state["items"])
            if endpoint == "movie/editor" and method == "PUT":
                item_ids = set(data["movieIds"])
                root = data["rootFolderPath"].rstrip("/")
                state["items"] = [
                    (
                        {
                            **item,
                            "path": f"{root}/{Path(item['path']).name}",
                        }
                        if item["id"] in item_ids
                        else item
                    )
                    for item in state["items"]
                ]
                return None
            if endpoint.startswith("movie/") and method == "PUT":
                item_id = int(endpoint.split("/", 1)[1].split("?", 1)[0])
                state["items"] = [
                    copy_json(data) if item["id"] == item_id else item
                    for item in state["items"]
                ]
                return data
            if endpoint == "downloadclient" and method == "GET":
                return copy_json(state["clients"])
            if endpoint.startswith("downloadclient/") and method == "PUT":
                client_id = int(endpoint.rsplit("/", 1)[1])
                state["clients"] = [
                    copy_json(data) if item["id"] == client_id else item
                    for item in state["clients"]
                ]
                return data
            if endpoint == "importlist" and method == "GET":
                return copy_json(state["import_lists"])
            if endpoint.startswith("importlist/") and method == "PUT":
                import_list_id = int(endpoint.split("/", 1)[1].split("?", 1)[0])
                state["import_lists"] = [
                    copy_json(data) if item["id"] == import_list_id else item
                    for item in state["import_lists"]
                ]
                return data
            if endpoint == "collection" and method == "GET":
                return copy_json(state["collections"])
            if endpoint == "collection" and method == "PUT":
                collection_ids = set(data["collectionIds"])
                state["collections"] = [
                    (
                        {**item, "rootFolderPath": data["rootFolderPath"]}
                        if item["id"] in collection_ids
                        else item
                    )
                    for item in state["collections"]
                ]
                return None
            if endpoint == "tag" and method == "GET":
                return copy_json(state["tags"])
            if endpoint.startswith("tag/") and method == "PUT":
                tag_id = int(endpoint.rsplit("/", 1)[1])
                state["tags"] = [
                    copy_json(data) if item["id"] == tag_id else item
                    for item in state["tags"]
                ]
                return data
            raise AssertionError((endpoint, method))

        manager = InfiniDyskMigrationManager()
        migrated = manager._desired_arr_snapshot(snapshot)
        with patch("utils.infinidysk_migration._arr_req", side_effect=fake_request):
            manager._apply_arr_snapshot(snapshot, migrated)
            manager._validate_arr_snapshot(snapshot, migrated)
            self.assertEqual(
                ["/mnt/debrid/infinidysk-symlinks/movies"],
                [root["path"] for root in state["roots"]],
            )
            self.assertEqual("InfiniDysk", state["clients"][0]["name"])
            self.assertEqual(
                "/mnt/debrid/infinidysk-symlinks/movies",
                state["import_lists"][0]["rootFolderPath"],
            )
            self.assertEqual("NzbDAV Watchlist", state["import_lists"][0]["name"])
            self.assertEqual(
                "/mnt/debrid/infinidysk-symlinks/movies",
                state["collections"][0]["rootFolderPath"],
            )
            self.assertEqual("NzbDAV Collection", state["collections"][0]["title"])
            self.assertEqual({"id": 3, "label": "InfiniDysk"}, state["tags"][0])
            state["roots"].append(
                {
                    "id": 99,
                    "path": "/mnt/debrid/infinidysk-symlinks/movies",
                }
            )
            manager._apply_arr_snapshot(migrated, snapshot)
            manager._validate_arr_snapshot(migrated, snapshot)

        self.assertEqual(
            ["/mnt/debrid/nzbdav-symlinks/movies"],
            [root["path"] for root in state["roots"]],
        )
        self.assertEqual("NzbDAV", state["clients"][0]["name"])
        self.assertEqual(
            "/mnt/debrid/nzbdav-symlinks/movies",
            state["import_lists"][0]["rootFolderPath"],
        )
        self.assertEqual(
            "/mnt/debrid/nzbdav-symlinks/movies",
            state["collections"][0]["rootFolderPath"],
        )
        self.assertEqual({"id": 3, "label": "NzbDAV"}, state["tags"][0])

    def test_prowlarr_applications_and_tags_round_trip_through_full_migration(self):
        snapshot = {
            "host": "http://127.0.0.1:9696",
            "api_key": "secret",
            "applications": [
                {
                    "id": 8,
                    "name": "Radarr (NzbDAV)",
                    "implementation": "Radarr",
                    "tags": [4],
                    "fields": [
                        {"name": "baseUrl", "value": "http://127.0.0.1:7879"},
                        {"name": "apiKey", "value": "arr-secret"},
                    ],
                }
            ],
            "tags": [{"id": 4, "label": "nzbdav"}],
        }
        state = {
            "applications": copy_json(snapshot["applications"]),
            "tags": copy_json(snapshot["tags"]),
        }

        def fake_request(url, _token, method="GET", data=None, **_kwargs):
            endpoint = url.split("/api/v1/", 1)[1]
            if endpoint == "applications" and method == "GET":
                return copy_json(state["applications"])
            if endpoint.startswith("applications/") and method == "PUT":
                application_id = int(endpoint.rsplit("/", 1)[1])
                state["applications"] = [
                    copy_json(data) if item["id"] == application_id else item
                    for item in state["applications"]
                ]
                return data
            if endpoint == "tag" and method == "GET":
                return copy_json(state["tags"])
            if endpoint.startswith("tag/") and method == "PUT":
                tag_id = int(endpoint.rsplit("/", 1)[1])
                state["tags"] = [
                    copy_json(data) if item["id"] == tag_id else item
                    for item in state["tags"]
                ]
                return data
            raise AssertionError((endpoint, method))

        manager = InfiniDyskMigrationManager()
        migrated = manager._desired_prowlarr_snapshot(snapshot)
        with patch(
            "utils.infinidysk_migration._prowlarr_req", side_effect=fake_request
        ):
            result = manager._apply_prowlarr_snapshot(snapshot, migrated)
            manager._validate_prowlarr_snapshot(snapshot, migrated)
            self.assertEqual({"applications": 1, "tags": 1}, result)
            self.assertEqual("Radarr (InfiniDysk)", state["applications"][0]["name"])
            self.assertEqual("infinidysk", state["tags"][0]["label"])
            manager._apply_prowlarr_snapshot(migrated, snapshot)
            manager._validate_prowlarr_snapshot(migrated, snapshot)

        self.assertEqual("Radarr (NzbDAV)", state["applications"][0]["name"])
        self.assertEqual("nzbdav", state["tags"][0]["label"])

    def test_prowlarr_preflight_blocks_legacy_canonical_name_collisions(self):
        snapshot = {
            "process_name": "Prowlarr Main",
            "applications": [
                {"id": 8, "name": "Radarr NzbDAV"},
                {"id": 9, "name": "Radarr InfiniDysk"},
            ],
            "tags": [
                {"id": 3, "label": "nzbdav"},
                {"id": 10, "label": "infinidysk"},
            ],
        }

        blockers = InfiniDyskMigrationManager._prowlarr_namespace_conflicts(snapshot)

        self.assertEqual(2, len(blockers))
        self.assertTrue(any("tag label 'infinidysk'" in item for item in blockers))
        self.assertTrue(
            any("application name 'radarr infinidysk'" in item for item in blockers)
        )
        self.assertTrue(all("run preflight again" in item for item in blockers))

    def test_full_namespace_failure_restores_paths_and_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "nzbdav"
            destination = root / "infinidysk"
            source.mkdir()
            config_path = root / "dumb_config.json"
            config = {
                "infinidysk": {
                    "enabled": False,
                    "process_name": "NzbDAV",
                    "repo_owner": "nzbdav",
                    "repo_name": "nzbdav",
                    "config_dir": str(source),
                    "log_file": "/log/other.log",
                    "symlink_backup_roots": [],
                }
            }
            original = json.loads(json.dumps(config))
            config_path.write_text(json.dumps({"nzbdav": config["infinidysk"]}))
            manager = InfiniDyskMigrationManager(root / "state.json")
            handler = MagicMock()
            handler.process_names = {}
            handler._prefixed_name.side_effect = lambda value: value
            manager_config = MagicMock()
            manager_config.config = config
            manager_config.file_path = config_path
            manager_config.uses_legacy_infinidysk_identity.return_value = True
            mapping = ((str(source), str(destination)),)
            with (
                patch("utils.infinidysk_migration.CONFIG_MANAGER", manager_config),
                patch("utils.infinidysk_migration.NAMESPACE_PATH_MAPPINGS", mapping),
                patch.object(manager, "_arr_snapshot", return_value=([], [], [])),
                patch.object(manager, "_media_snapshot", return_value=([], [])),
                patch.object(
                    manager, "_infinidysk_active_reads", return_value=(0, None)
                ),
                patch.object(
                    manager,
                    "_migrate_infinidysk_database",
                    side_effect=RuntimeError("simulated failure"),
                ),
            ):
                preflight = manager.preflight(handler, MagicMock())
                with self.assertRaises(InfiniDyskMigrationError):
                    manager.apply_full_namespace(
                        preflight["token"], True, handler, MagicMock()
                    )

            self.assertTrue(source.is_dir())
            self.assertFalse(destination.exists())
            self.assertEqual(original, manager_config.config)
            manager_config.restore_legacy_infinidysk_identity.assert_called()

    def test_full_namespace_rollback_restores_every_running_core_consumer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "nzbdav"
            destination = root / "infinidysk"
            source.mkdir()
            config_path = root / "dumb_config.json"
            config = {
                "infinidysk": {
                    "enabled": True,
                    "process_name": "NzbDAV",
                    "repo_owner": "nzbdav",
                    "repo_name": "nzbdav",
                    "config_dir": str(source),
                    "log_file": "/log/other.log",
                    "symlink_backup_roots": [],
                }
            }
            consumers = {
                "rclone": ("Rclone w/ NzbDAV", {"key_type": "nzbdav"}),
                "radarr": ("Radarr NzbDAV", {"core_service": "nzbdav"}),
                "sonarr": (
                    "Sonarr Combined",
                    {"core_services": ["decypharr, nzbdav"]},
                ),
                "lidarr": ("Lidarr NzbDAV", {"core_service": "nzbdav"}),
                "whisparr": ("Whisparr NzbDAV", {"core_service": "nzbdav"}),
                "neutarr": ("NeutArr NzbDAV", {"core_service": "nzbdav"}),
                "profilarr": ("Profilarr NzbDAV", {"core_service": "nzbdav"}),
                "seerr": ("Seerr NzbDAV", {"core_service": "nzbdav"}),
            }
            for service_key, (process_name, linkage) in consumers.items():
                config[service_key] = {
                    "instances": {
                        "NzbDAV": {
                            "enabled": True,
                            "process_name": process_name,
                            **linkage,
                        }
                    }
                }
            config["zurg"] = {
                "instances": {
                    "Unused": {
                        "enabled": True,
                        "process_name": "Zurg Usenet",
                        "core_service": "nzbdav",
                    }
                }
            }
            original = copy_json(config)
            config_path.write_text(
                json.dumps({"nzbdav": config["infinidysk"]}), encoding="utf-8"
            )

            running = {
                "NzbDAV",
                *(process_name for process_name, _linkage in consumers.values()),
                "Zurg Usenet",
            }

            class Process:
                def __init__(self, name):
                    self.name = name

                def poll(self):
                    return None if self.name in running else 0

            handler = MagicMock()
            handler.process_names = {name: Process(name) for name in running}
            handler._prefixed_name.side_effect = lambda value: value
            handler.stop_process.side_effect = lambda name: running.discard(name)

            manager = InfiniDyskMigrationManager(root / "state.json")
            manager_config = MagicMock()
            manager_config.config = config
            manager_config.file_path = config_path
            manager_config.uses_legacy_infinidysk_identity.return_value = True
            start_calls = []

            def fail_new_start(
                _handler,
                process_names,
                *,
                force_setup=True,
                defer_provider_integrations=False,
                continue_on_error=False,
            ):
                start_calls.append(
                    (
                        list(process_names),
                        force_setup,
                        defer_provider_integrations,
                        continue_on_error,
                    )
                )
                if force_setup:
                    raise RuntimeError("simulated dependent restart failure")
                return []

            def quiesce_all(
                _config,
                preflight,
                old_process_names,
                process_handler,
                _logger,
                _progress,
                stopped,
                _scan_guards,
                job_id=None,
            ):
                stopped.extend(
                    manager._stop_processes(process_handler, old_process_names)
                )
                return preflight

            with (
                patch("utils.infinidysk_migration.CONFIG_MANAGER", manager_config),
                patch(
                    "utils.infinidysk_migration.NAMESPACE_PATH_MAPPINGS",
                    ((str(source), str(destination)),),
                ),
                patch.object(manager, "_arr_snapshot", return_value=([], [], [])),
                patch.object(manager, "_media_snapshot", return_value=([], [])),
                patch.object(
                    manager, "_infinidysk_active_reads", return_value=(0, None)
                ),
                patch.object(
                    InfiniDyskMigrationManager,
                    "_infinidysk_config_item",
                    return_value={
                        "present": True,
                        "value": "false",
                        "environment_variable_name": "",
                    },
                ),
                patch.object(manager, "_quiesce_for_cutover", side_effect=quiesce_all),
                patch.object(manager, "_migrate_infinidysk_database", return_value=0),
                patch.object(manager, "_start_processes", side_effect=fail_new_start),
            ):
                preflight = manager.preflight(handler, MagicMock())
                with self.assertRaisesRegex(InfiniDyskMigrationError, "rolled back"):
                    manager.apply_full_namespace(
                        preflight["token"], True, handler, MagicMock()
                    )

            expected_old = [
                "NzbDAV",
                *(process_name for process_name, _linkage in consumers.values()),
            ]
            expected_new = [
                "InfiniDysk",
                *(
                    process_name.replace("NzbDAV", "InfiniDysk")
                    for process_name, _linkage in consumers.values()
                ),
            ]
            producer_names = {
                "NeutArr InfiniDysk",
                "Profilarr InfiniDysk",
                "Seerr InfiniDysk",
            }
            expected_new_without_producers = [
                name for name in expected_new if name not in producer_names
            ]
            self.assertEqual(
                (expected_new_without_producers, True, True, False), start_calls[0]
            )
            self.assertEqual((expected_old, False, False, True), start_calls[-1])
            self.assertNotIn("Zurg Usenet", start_calls[0][0])
            self.assertNotIn("Zurg Usenet", start_calls[-1][0])
            self.assertEqual(original, manager_config.config)
            self.assertTrue(source.is_dir())
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
