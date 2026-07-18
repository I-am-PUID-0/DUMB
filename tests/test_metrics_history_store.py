import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from utils.metrics_history_store import (
    MetricsHistoryManager,
    SQLiteMetricsHistoryStore,
)
from utils.metrics_postgres import (
    activate_metrics_postgresql,
    ensure_metrics_postgres_config,
)


def _snapshot(timestamp, process_count=4):
    return {
        "timestamp": float(timestamp),
        "system": {
            "cpu_percent": 12.5,
            "cpu_count": 8,
            "mem": {"percent": 42.0, "total": 1024, "available": 512},
            "disk": {"percent": 31.0, "total": 2048, "free": 1024},
            "inode": {"percent": 8.0, "total": 1000, "free": 920},
            "disk_io": {"read_bytes": 1000, "write_bytes": 2000},
            "net_io": {"sent_bytes": 3000, "recv_bytes": 4000},
        },
        "dumb_managed": [
            {
                "name": f"Service {index}",
                "pid": 100 + index,
                "cpu_percent": 1.5,
                "rss": 500000,
                "disk_io": {"read_bytes": 100, "write_bytes": 200},
            }
            for index in range(process_count)
        ],
        "external": [],
        "database_health": {"services": []},
    }


class _ConfigManager:
    def __init__(self, history_dir, provider="sqlite"):
        self.config = {
            "dumb": {
                "metrics": {
                    "history_enabled": True,
                    "history_interval_sec": 5,
                    "history_retention_days": 7,
                    "history_max_file_mb": 50,
                    "history_max_total_mb": 100,
                    "history_dir": history_dir,
                    "storage": {
                        "provider": provider,
                        "sqlite_path": os.path.join(history_dir, "metrics.sqlite"),
                        "migrate_jsonl": True,
                        "postgresql": {
                            "database": "dumb_metrics",
                            "schema": "public",
                            "local_retention_days": 7,
                            "retry_interval_sec": 60,
                        },
                    },
                }
            },
            "postgres": {
                "enabled": False,
                "process_name": "PostgreSQL",
                "host": "127.0.0.1",
                "port": 5432,
                "user": "DUMB",
                "password": "postgres",
                "databases": [{"name": "postgres", "enabled": True}],
            },
        }
        self.save_calls = 0

    def save_config(self):
        self.save_calls += 1


class _FailingPostgresStore:
    def latest_timestamp(self):
        raise RuntimeError("database offline")

    def write_many(self, _items):
        raise RuntimeError("database offline")


class _MemoryPostgresStore:
    def __init__(self):
        self.items = []

    def latest_timestamp(self):
        if not self.items:
            return None
        return self.items[-1]["timestamp"]

    def write_many(self, items):
        by_timestamp = {item["timestamp"]: item for item in self.items}
        by_timestamp.update({item["timestamp"]: item for item in items})
        self.items = [by_timestamp[key] for key in sorted(by_timestamp)]
        return len(items)

    def prune(self, retention_days=0, max_total_mb=0):
        return 0

    def read(self, since=None, limit=5000):
        items = [
            item for item in self.items if since is None or item["timestamp"] >= since
        ]
        return items[-limit:] if limit else items

    def status(self):
        return {"samples": len(self.items), "database": "dumb_metrics"}


class MetricsHistoryStoreTests(unittest.TestCase):
    def test_postgresql_config_enables_service_and_registers_metrics_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _ConfigManager(temp_dir, provider="postgresql")

            changed = ensure_metrics_postgres_config(config)

            self.assertTrue(changed)
            self.assertTrue(config.config["postgres"]["enabled"])
            self.assertIn(
                {"name": "dumb_metrics", "enabled": True},
                config.config["postgres"]["databases"],
            )

    def test_sqlite_round_trip_uses_compressed_payloads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMetricsHistoryStore(os.path.join(temp_dir, "metrics.sqlite"))
            expected = _snapshot(1000, process_count=20)

            store.write(expected)

            self.assertEqual(store.read(), [expected])
            status = store.status()
            self.assertEqual(status["samples"], 1)
            self.assertLess(status["compressed_bytes"], status["raw_bytes"])
            self.assertLess(status["compression_ratio"], 1)

    def test_sqlite_read_returns_latest_limit_in_time_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMetricsHistoryStore(os.path.join(temp_dir, "metrics.sqlite"))
            for timestamp in range(10):
                store.write(_snapshot(timestamp))

            items = store.read(limit=3)

            self.assertEqual([item["timestamp"] for item in items], [7.0, 8.0, 9.0])

    def test_retention_maintenance_is_throttled_and_reapplies_config_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_manager = _ConfigManager(temp_dir)
            manager = MetricsHistoryManager(config_manager)
            manager._configure()
            manager._sqlite.prune = Mock()
            now = time.time()

            manager.write(_snapshot(now))
            manager.write(_snapshot(now + 5))

            manager._sqlite.prune.assert_called_once()

            config_manager.config["dumb"]["metrics"]["history_retention_days"] = 14
            manager.write(_snapshot(now + 10))

            self.assertEqual(manager._sqlite.prune.call_count, 2)
            self.assertEqual(
                manager._sqlite.prune.call_args.kwargs["retention_days"], 14
            )

    def test_jsonl_migration_is_idempotent_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "metrics-20260717-000.jsonl")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(_snapshot(1000)) + "\n")
                handle.write(json.dumps(_snapshot(1005)) + "\n")
            manager = MetricsHistoryManager(_ConfigManager(temp_dir))

            first = manager.migrate_legacy()
            second = manager.migrate_legacy()

            self.assertTrue(os.path.exists(source))
            self.assertEqual(first["samples"], 2)
            self.assertEqual(second["samples"], 2)
            self.assertEqual(manager.status()["sqlite"]["samples"], 2)

    def test_failed_jsonl_batch_is_not_marked_complete_and_retries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = MetricsHistoryManager(_ConfigManager(temp_dir))
            manager._configure()
            source = os.path.join(temp_dir, "metrics-20260717-000.jsonl")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(_snapshot(1000)) + "\n")

            with patch.object(
                manager._sqlite,
                "write_many",
                side_effect=OSError("simulated write failure"),
            ):
                failed = manager.migrate_legacy(force=True)

            self.assertFalse(failed["completed"])
            self.assertEqual(failed["skipped"], 1)
            self.assertEqual(
                manager._sqlite.metadata("jsonl_migration_v1"), "incomplete"
            )

            retried = manager.migrate_legacy()

            self.assertTrue(retried["completed"])
            self.assertEqual(retried["samples"], 1)
            self.assertEqual(manager._sqlite.metadata("jsonl_migration_v1"), "complete")
            self.assertEqual(manager.status()["sqlite"]["samples"], 1)

    def test_malformed_jsonl_record_keeps_migration_incomplete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "metrics-20260717-000.jsonl")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(_snapshot(1000)) + "\n")
                handle.write('{"timestamp": 1005\n')
            manager = MetricsHistoryManager(_ConfigManager(temp_dir))

            result = manager.migrate_legacy()

            self.assertFalse(result["completed"])
            self.assertEqual(result["samples"], 1)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual(
                manager._sqlite.metadata("jsonl_migration_v1"), "incomplete"
            )
            self.assertTrue(os.path.exists(source))

    def test_postgresql_failure_keeps_sqlite_history_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = MetricsHistoryManager(
                _ConfigManager(temp_dir, provider="postgresql")
            )
            manager._configure()
            manager._postgres = _FailingPostgresStore()

            manager.write(_snapshot(time.time()))
            items, truncated = manager.read(full=True)
            status = manager.status()

            self.assertEqual(len(items), 1)
            self.assertFalse(truncated)
            self.assertEqual(status["active_provider"], "sqlite")
            self.assertTrue(status["fallback_active"])
            self.assertIn("database offline", status["last_error"])

    def test_postgresql_mode_replays_local_samples_before_reads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = MetricsHistoryManager(
                _ConfigManager(temp_dir, provider="postgresql")
            )
            manager._configure()
            postgres = _MemoryPostgresStore()
            manager._postgres = postgres
            now = time.time()

            manager.write(_snapshot(now))
            manager.write(_snapshot(now + 5))
            items, _truncated = manager.read(full=True)

            self.assertEqual(
                [item["timestamp"] for item in postgres.items], [now, now + 5]
            )
            self.assertEqual([item["timestamp"] for item in items], [now, now + 5])
            self.assertEqual(manager.status()["active_provider"], "postgresql")

    def test_explicit_postgresql_activation_replays_before_promotion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = MetricsHistoryManager(
                _ConfigManager(temp_dir, provider="postgresql")
            )
            manager._configure()
            postgres = _MemoryPostgresStore()
            manager._postgres = postgres
            manager._sqlite.write(_snapshot(1000))
            manager._sqlite.write(_snapshot(1005))

            result = manager.activate_postgresql()

            self.assertEqual(result["synced_samples"], 2)
            self.assertEqual(result["active_provider"], "postgresql")
            self.assertFalse(result["fallback_active"])
            self.assertEqual(
                [item["timestamp"] for item in postgres.items], [1000.0, 1005.0]
            )

    def test_postgresql_activation_reconciles_older_sqlite_samples(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = MetricsHistoryManager(
                _ConfigManager(temp_dir, provider="postgresql")
            )
            manager._configure()
            postgres = _MemoryPostgresStore()
            postgres.write_many([_snapshot(2000)])
            manager._postgres = postgres
            manager._sqlite.write(_snapshot(1000))

            result = manager.activate_postgresql()

            self.assertEqual(result["synced_samples"], 1)
            self.assertEqual(
                [item["timestamp"] for item in postgres.items], [1000.0, 2000.0]
            )

    def test_postgresql_recovery_reconciles_before_read_promotion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = MetricsHistoryManager(
                _ConfigManager(temp_dir, provider="postgresql")
            )
            manager._configure()
            postgres = _MemoryPostgresStore()
            postgres.write_many([_snapshot(2000)])
            manager._postgres = postgres
            manager._sqlite.write(_snapshot(1000))
            manager._active_provider = "sqlite"

            items, _truncated = manager.read(full=True)

            self.assertEqual([item["timestamp"] for item in items], [1000.0, 2000.0])
            self.assertEqual(manager._active_provider, "postgresql")

    def test_forced_jsonl_import_reconciles_before_postgresql_resumes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "metrics-20260717-000.jsonl")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(_snapshot(1000)) + "\n")
            manager = MetricsHistoryManager(
                _ConfigManager(temp_dir, provider="postgresql")
            )
            manager._configure()
            postgres = _MemoryPostgresStore()
            postgres.write_many([_snapshot(2000)])
            manager._postgres = postgres
            manager._active_provider = "postgresql"

            manager.migrate_legacy(force=True)
            items, _truncated = manager.read(full=True)

            self.assertEqual([item["timestamp"] for item in items], [1000.0, 2000.0])
            self.assertEqual(manager._active_provider, "postgresql")

    @patch("utils.metrics_postgres.initialize_postgres_databases")
    @patch("utils.metrics_postgres.setup_project")
    def test_hot_activation_starts_postgres_without_restart(
        self, setup_project_mock, initialize_databases_mock
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _ConfigManager(temp_dir, provider="postgresql")
            setup_project_mock.return_value = (True, None)
            process_handler = Mock()
            process_handler.setup_tracker = set()
            process_handler.setup_tracker_lock = threading.Lock()
            api_state = Mock()
            api_state.get_status.return_value = "stopped"
            history_manager = Mock()
            history_manager.activate_postgresql.return_value = {
                "synced_samples": 3,
                "active_provider": "postgresql",
                "fallback_active": False,
                "postgresql": {"samples": 3},
            }

            result = activate_metrics_postgresql(
                config,
                process_handler,
                api_state,
                history_manager,
                Mock(),
            )

            setup_project_mock.assert_called_once_with(process_handler, "PostgreSQL")
            initialize_databases_mock.assert_not_called()
            history_manager.activate_postgresql.assert_called_once_with()
            self.assertEqual(result["status"], "active")
            self.assertEqual(result["synced_samples"], 3)
            self.assertTrue(result["postgres_started"])
            self.assertFalse(result["postgres_reused"])
            self.assertEqual(config.save_calls, 1)

    @patch("utils.metrics_postgres.initialize_postgres_databases")
    @patch("utils.metrics_postgres.setup_project")
    def test_hot_activation_reuses_running_postgres(
        self, setup_project_mock, initialize_databases_mock
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = _ConfigManager(temp_dir, provider="postgresql")
            initialize_databases_mock.return_value = (True, None)
            process_handler = Mock()
            process_handler.setup_tracker = set()
            process_handler.setup_tracker_lock = threading.Lock()
            api_state = Mock()
            api_state.get_status.return_value = "running"
            history_manager = Mock()
            history_manager.activate_postgresql.return_value = {
                "synced_samples": 0,
                "active_provider": "postgresql",
                "fallback_active": False,
                "postgresql": {"samples": 10},
            }

            result = activate_metrics_postgresql(
                config,
                process_handler,
                api_state,
                history_manager,
                Mock(),
            )

            setup_project_mock.assert_not_called()
            initialize_databases_mock.assert_called_once()
            self.assertEqual(result["status"], "active")
            self.assertFalse(result["postgres_started"])
            self.assertTrue(result["postgres_reused"])


if __name__ == "__main__":
    unittest.main()
