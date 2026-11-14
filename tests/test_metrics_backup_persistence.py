import ast
import asyncio
import gc
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from utils import infinidysk_migration_admission as admission
from utils.installation_backup import installation_persistence_work
from utils.metrics_history_store import (
    MetricsHistoryBusyError,
    MetricsHistoryManager,
    SQLiteMetricsHistoryStore,
)


def history_config(directory):
    return {
        "dumb": {
            "metrics": {
                "history_dir": str(directory),
                "storage": {
                    "provider": "sqlite",
                    "sqlite_path": str(directory / "metrics.sqlite"),
                    "migrate_jsonl": False,
                },
            }
        }
    }


def cold_files(directory):
    return {
        path.name: (
            path.stat().st_mtime_ns,
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in directory.iterdir()
    }, directory.stat().st_mtime_ns


class MetricsConnectionLifetimeTests(unittest.TestCase):
    def test_all_transactions_close_before_later_gc_can_change_cold_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            enabled = gc.isenabled()
            gc.disable()
            try:
                store = SQLiteMetricsHistoryStore(directory / "metrics.sqlite")
                store.write_many([{"timestamp": 1}, {"timestamp": 2}])
                self.assertEqual(len(store.read()), 2)
                self.assertEqual(len(store.read_forward()), 2)
                self.assertEqual(store.latest_timestamp(), 2)
                store.prune()
                store.set_metadata("fixture", "complete")
                self.assertEqual(store.metadata("fixture"), "complete")
                self.assertEqual(store.status()["samples"], 2)
                self.assertEqual(
                    {path.name for path in directory.iterdir()}, {"metrics.sqlite"}
                )
                before = cold_files(directory)

                gc.collect()

                self.assertEqual(cold_files(directory), before)
            finally:
                if enabled:
                    gc.enable()

    def test_failed_body_rolls_back_and_closes_the_real_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store = SQLiteMetricsHistoryStore(directory / "metrics.sqlite")
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                with store._connect() as connection:
                    connection.execute(
                        "INSERT INTO metrics_metadata(key, value) VALUES (?, ?)",
                        ("fixture", "uncommitted"),
                    )
                    raise RuntimeError("fixture failure")

            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
            self.assertIsNone(store.metadata("fixture"))
            self.assertEqual(
                {path.name for path in directory.iterdir()}, {"metrics.sqlite"}
            )

    def test_pragma_failure_also_closes_the_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = SQLiteMetricsHistoryStore(Path(temporary) / "metrics.sqlite")
            connection = Mock()
            connection.execute.side_effect = sqlite3.OperationalError("fixture")
            with (
                patch(
                    "utils.metrics_history_store.sqlite3.connect",
                    return_value=connection,
                ),
                self.assertRaises(sqlite3.OperationalError),
            ):
                with store._connect():
                    self.fail("PRAGMA failure must not yield a connection")
            connection.close.assert_called_once_with()


class MetricsBackupAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.blocked = self.enterContext(
            patch.object(admission, "_installation_blocked", return_value=False)
        )
        self.enterContext(patch.object(admission, "_EXTERNAL_MUTATIONS", {}))

    def test_every_public_disk_operation_refuses_before_opening_a_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "not-created"
            manager = MetricsHistoryManager(history_config(directory))
            manager._configure = Mock(side_effect=AssertionError("disk I/O admitted"))
            self.blocked.return_value = True
            operations = [
                lambda: manager.write({"timestamp": 1}),
                manager.read,
                manager.status,
                manager.migrate_legacy,
                manager.activate_postgresql,
            ]
            for operation in operations:
                with (
                    self.subTest(operation=operation),
                    self.assertRaises(MetricsHistoryBusyError),
                ):
                    operation()
            manager._configure.assert_not_called()
            self.assertFalse(directory.exists())
            self.assertEqual(admission._EXTERNAL_MUTATIONS, {})

    def test_store_connections_close_while_the_admission_reservation_is_held(self):
        closed_during_reservation = []
        original_connect = sqlite3.connect

        class ObservedConnection(sqlite3.Connection):
            def close(self):
                closed_during_reservation.append(
                    admission.infinidysk_external_mutation_active()
                )
                return super().close()

        def connect(*args, **kwargs):
            return original_connect(*args, factory=ObservedConnection, **kwargs)

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("utils.metrics_history_store.sqlite3.connect", side_effect=connect),
        ):
            manager = MetricsHistoryManager(history_config(Path(temporary)))
            manager.write({"timestamp": 1})
            manager.read(full=True)
            manager.status()
            manager.migrate_legacy()
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                manager.activate_postgresql()

        self.assertTrue(closed_during_reservation)
        self.assertTrue(all(closed_during_reservation))
        self.assertFalse(admission.infinidysk_external_mutation_active())

    def test_nested_worker_reservation_survives_manager_operation_and_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = MetricsHistoryManager(history_config(Path(temporary)))
            with installation_persistence_work("Outer metrics worker") as admitted:
                self.assertTrue(admitted)
                outer_tokens = set(admission._EXTERNAL_MUTATIONS)
                manager.write({"timestamp": 1})
                self.assertEqual(set(admission._EXTERNAL_MUTATIONS), outer_tokens)
                with patch.object(
                    manager._sqlite, "status", side_effect=OSError("fixture")
                ):
                    with self.assertRaises(OSError):
                        manager.status()
                self.assertEqual(set(admission._EXTERNAL_MUTATIONS), outer_tokens)
            self.assertFalse(admission.infinidysk_external_mutation_active())


def source_functions(relative, names, namespace):
    source = Path(__file__).resolve().parents[1] / relative
    tree = ast.parse(source.read_text())
    selected = []
    for node in tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in names
        ):
            node.decorator_list = []
            selected.append(node)
    assert len(selected) == len(names)
    # Isolate fixed, checked-in route functions from runtime configuration.
    exec(  # noqa: S102
        compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"),
        namespace,
    )
    return namespace


async def threadpool(operation, *args, **kwargs):
    return operation(*args, **kwargs)


class MetricsBusyTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_history_busy_is_an_explicit_conflict(self):
        namespace = source_functions(
            "api/routers/metrics.py",
            {"_history_request"},
            {
                "run_in_threadpool": threadpool,
                "MetricsHistoryBusyError": MetricsHistoryBusyError,
                "HTTPException": HTTPException,
            },
        )
        operation = Mock(side_effect=MetricsHistoryBusyError("History is paused."))
        with self.assertRaises(HTTPException) as caught:
            await namespace["_history_request"](operation)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail, "History is paused.")

    async def test_websocket_history_busy_closes_cleanly_and_disconnects(self):
        class Disconnected(Exception):
            pass

        namespace = {
            "asyncio": asyncio,
            "json": json,
            "WebSocket": object,
            "Depends": lambda operation: operation,
            "MetricsHistoryBusyError": MetricsHistoryBusyError,
            "WebSocketDisconnect": Disconnected,
            "run_in_threadpool": threadpool,
            "_ensure_publisher": AsyncMock(),
        }
        for name in [
            "get_metrics_collector",
            "get_metrics_history_manager",
            "get_metrics_manager",
            "get_websocket_current_user",
        ]:
            namespace[name] = lambda: None
        source_functions(
            "api/routers/websocket_metrics.py",
            {"websocket_metrics", "_parse_bool", "_parse_int", "_parse_float"},
            namespace,
        )
        websocket = Mock(query_params={"bootstrap": "true"})
        websocket.close = AsyncMock()
        manager = Mock(connect=AsyncMock(), disconnect=AsyncMock())
        history = Mock(read=Mock(side_effect=MetricsHistoryBusyError("paused")))

        await namespace["websocket_metrics"](
            websocket,
            collector=Mock(),
            history_manager=history,
            metrics_manager=manager,
        )

        self.assertEqual(websocket.close.await_args.kwargs["code"], 1013)
        manager.disconnect.assert_awaited_once_with(websocket)
        namespace["_ensure_publisher"].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
