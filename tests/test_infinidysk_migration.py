import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from utils.infinidysk_migration import (
    ARR_SERVICE_API,
    InfiniDyskMigrationError,
    InfiniDyskMigrationManager,
    _config_fingerprint,
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
                progress_callback=None,
            ):
                self.assertEqual(token, preflight_token)
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
                patch.object(manager, "_arr_snapshot", return_value=([], [])),
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

    def test_arr_paths_clients_and_roots_round_trip_through_full_migration(self):
        snapshot = {
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
            "tags": [{"id": 3, "label": "NzbDAV"}],
        }
        state = {
            "roots": copy_json(snapshot["roots"]),
            "items": copy_json(snapshot["items"]),
            "clients": copy_json(snapshot["clients"]),
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
            self.assertEqual({"id": 3, "label": "InfiniDysk"}, state["tags"][0])
            manager._apply_arr_snapshot(migrated, snapshot)
            manager._validate_arr_snapshot(migrated, snapshot)

        self.assertEqual(
            ["/mnt/debrid/nzbdav-symlinks/movies"],
            [root["path"] for root in state["roots"]],
        )
        self.assertEqual("NzbDAV", state["clients"][0]["name"])
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
                patch.object(manager, "_arr_snapshot", return_value=([], [])),
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
            ):
                start_calls.append(
                    (
                        list(process_names),
                        force_setup,
                        defer_provider_integrations,
                    )
                )
                if force_setup:
                    raise RuntimeError("simulated dependent restart failure")

            with (
                patch("utils.infinidysk_migration.CONFIG_MANAGER", manager_config),
                patch(
                    "utils.infinidysk_migration.NAMESPACE_PATH_MAPPINGS",
                    ((str(source), str(destination)),),
                ),
                patch.object(manager, "_arr_snapshot", return_value=([], [])),
                patch.object(manager, "_media_snapshot", return_value=([], [])),
                patch.object(
                    manager, "_infinidysk_active_reads", return_value=(0, None)
                ),
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
            self.assertEqual((expected_new, True, True), start_calls[0])
            self.assertEqual((expected_old, False, False), start_calls[-1])
            self.assertNotIn("Zurg Usenet", start_calls[0][0])
            self.assertNotIn("Zurg Usenet", start_calls[-1][0])
            self.assertEqual(original, manager_config.config)
            self.assertTrue(source.is_dir())
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
