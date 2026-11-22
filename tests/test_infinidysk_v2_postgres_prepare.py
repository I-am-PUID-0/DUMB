import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import psycopg2
from psycopg2 import sql

from utils.infinidysk_v2_postgres import (
    InfiniDyskV2PostgresManager,
    NativePostgresError,
)
from utils.infinidysk_v2_postgres_prepare import prepare_managed_target


class Config:
    def __init__(self, value, path):
        self.config = value
        self.file_path = path

    def get(self, key):
        return self.config.get(key)


class ManagedDestinationTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.data = self.root / "postgres"
        self.data.mkdir()
        (self.data / "postmaster.pid").write_text(f"123\n{self.data}\n1000\n5432\n")
        self.postgres = {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 5432,
            "user": "example_owner",
            "password": "fixture-only-secret",
            "config_dir": str(self.data),
            "databases": [{"name": "other_service", "enabled": False}],
        }
        self.config = Config(
            {
                "postgres": self.postgres,
                "infinidysk": {"postgres_database": "old_infinidysk"},
                "arrs": {"instances": {"example": {"postgres_main_db": "arr_main"}}},
            },
            self.root / "config.json",
        )
        self.process = SimpleNamespace(pid=123, poll=lambda: None)
        self.manager = SimpleNamespace(
            _config=lambda: self.config,
            _service=lambda: {"runtime_generation": "2"},
            process_handler=SimpleNamespace(
                _prefixed_name=lambda name: name,
                process_names={"PostgreSQL": self.process},
            ),
            target_inspector=self.inspect,
        )
        self.options = {
            "use_managed_postgres": True,
            "target": {"database": "example_native_v2"},
        }
        self.calls = []
        self.connections = []
        self.created = False
        self.existing = None
        self.pristine = True
        self.lock = True
        self.can_create = True
        self.cluster = ("11", 160005, str(self.data), 456, "127.0.0.1", 5432)
        self.identity = {
            "system_identifier": "11",
            "database_oid": "22",
            "schema": "public",
            "owner": "example_owner",
            "collation": "C",
            "relations": [],
        }
        update_state = patch(
            "utils.infinidysk_v2_update.native_update_state", return_value=None
        )
        update_state.start()
        self.addCleanup(update_state.stop)
        process_lookup = patch(
            "utils.infinidysk_v2_postgres_prepare.psutil.Process",
            side_effect=lambda pid: SimpleNamespace(
                pid=pid, children=lambda **kwargs: [], ppid=lambda: 123
            ),
        )
        process_lookup.start()
        self.addCleanup(process_lookup.stop)

    def inspect(self, target, *, require_empty=False):
        self.assertTrue(require_empty)
        self.assertEqual(target["hostaddr"], "127.0.0.1")
        return copy.deepcopy(self.identity)

    def connect(self, **kwargs):
        owner = self
        connection = SimpleNamespace(
            options=kwargs,
            autocommit=False,
            close=Mock(),
            set_session=Mock(),
        )

        class Cursor:
            result = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, query, parameters=None):
                owner.calls.append((query, parameters))
                if isinstance(query, sql.Composed):
                    owner.created = True
                    owner.existing = (
                        "22",
                        "example_owner",
                        "UTF8",
                        "C",
                        "C",
                        "c",
                        True,
                        False,
                    )
                elif "pg_control_system" in query:
                    self.result = owner.cluster
                elif "pg_try_advisory_lock" in query:
                    self.result = (owner.lock,)
                elif "rolcanlogin" in query:
                    self.result = ("example_owner", True, owner.can_create)
                elif "pg_database d" in query:
                    self.result = owner.existing
                elif query.startswith("SELECT EXISTS"):
                    self.result = (not owner.pristine,)
                else:
                    raise AssertionError(f"Unexpected query {query}")

            def fetchone(self):
                return self.result

        connection.cursor = Cursor
        self.connections.append(connection)
        return connection

    def prepare(self):
        return prepare_managed_target(self.manager, self.options, connect=self.connect)

    def test_create_pristine_owned_c_locale_database_then_inspect_identity(self):
        result = self.prepare()
        self.assertTrue(result["created"])
        self.assertEqual(result["target"]["database"], "example_native_v2")
        self.assertEqual(result["target_identity"]["system_identifier"], "11")
        cluster_query = next(
            query
            for query, _ in self.calls
            if isinstance(query, str) and "pg_control_system" in query
        )
        self.assertIn("pg_catalog.host(pg_catalog.inet_server_addr())", cluster_query)
        self.assertNotIn("inet_server_addr()::text", cluster_query)
        statement = next(
            query for query, _ in self.calls if isinstance(query, sql.Composed)
        )
        identifiers = [
            item.string for item in statement.seq if isinstance(item, sql.Identifier)
        ]
        self.assertEqual(identifiers, ["example_native_v2", "example_owner"])
        literal = "".join(
            item.string for item in statement.seq if isinstance(item, sql.SQL)
        )
        for clause in (
            "TEMPLATE template0",
            "ENCODING 'UTF8'",
            "LC_COLLATE 'C'",
            "LC_CTYPE 'C'",
            "LOCALE_PROVIDER libc",
        ):
            self.assertIn(clause, literal)
        self.assertTrue(all(connection.close.called for connection in self.connections))
        self.assertNotIn("fixture-only-secret", str(result))

    def test_reuse_empty_compatible_database_is_idempotent(self):
        self.prepare()
        self.calls.clear()
        result = self.prepare()
        self.assertFalse(result["created"])
        self.assertFalse(
            any(isinstance(query, sql.Composed) for query, _ in self.calls)
        )

    def test_reject_existing_database_with_different_owner_locale_or_policy(self):
        for index, value in (
            (1, "other_owner"),
            (2, "LATIN1"),
            (3, "en_US.UTF-8"),
            (4, "POSIX"),
            (5, "i"),
            (6, False),
            (7, True),
        ):
            with self.subTest(index=index):
                row = ["22", "example_owner", "UTF8", "C", "C", "c", True, False]
                row[index] = value
                self.existing = tuple(row)
                with self.assertRaisesRegex(NativePostgresError, "will not alter"):
                    self.prepare()
                self.assertFalse(self.created)

    def test_reject_database_containing_any_objects_without_cleanup(self):
        self.existing = ("22", "example_owner", "UTF8", "C", "C", "c", True, False)
        self.pristine = False
        with self.assertRaisesRegex(NativePostgresError, "will not empty"):
            self.prepare()
        self.assertFalse(self.created)
        self.assertFalse(
            any(isinstance(query, sql.Composed) for query, _ in self.calls)
        )

    def test_reject_system_and_configured_service_names_before_connecting(self):
        for name in (
            "postgres",
            "template0",
            "template1",
            "other_service",
            "old_infinidysk",
            "arr_main",
        ):
            with self.subTest(name=name):
                self.options["target"]["database"] = name
                with self.assertRaisesRegex(NativePostgresError, "reserved"):
                    self.prepare()
        self.assertEqual(self.connections, [])

    def test_reject_external_or_nonmanaged_target_without_connecting(self):
        self.options["use_managed_postgres"] = False
        with self.assertRaisesRegex(NativePostgresError, "only its own"):
            self.prepare()
        self.options["use_managed_postgres"] = True
        self.postgres["host"] = "db.example.invalid"
        with self.assertRaisesRegex(NativePostgresError, "external"):
            self.prepare()
        self.assertEqual(self.connections, [])

    def test_reject_sql_in_database_name_before_connecting(self):
        self.options["target"]["database"] = "example; DROP DATABASE other_service"
        with self.assertRaises(NativePostgresError):
            self.prepare()
        self.assertEqual(self.connections, [])

    def test_cannot_override_managed_endpoint_with_request_fields(self):
        self.options["target"]["host"] = "db.example.invalid"
        with self.assertRaisesRegex(NativePostgresError, "cannot override"):
            self.prepare()
        self.assertEqual(self.connections, [])

    def test_refuse_disabled_missing_or_exited_managed_process(self):
        self.postgres["enabled"] = False
        with self.assertRaisesRegex(NativePostgresError, "Enable and start"):
            self.prepare()
        self.postgres["enabled"] = True
        self.process.poll = lambda: 1
        with self.assertRaisesRegex(NativePostgresError, "Start DUMB"):
            self.prepare()
        self.manager.process_handler.process_names = {}
        with self.assertRaisesRegex(NativePostgresError, "Start DUMB"):
            self.prepare()
        self.assertEqual(self.connections, [])

    def test_reject_wrong_cluster_before_creating(self):
        original = list(self.cluster)
        for index, value in (
            (0, "unknown"),
            (1, 140001),
            (2, str(self.root / "elsewhere")),
            (4, "192.0.2.1"),
            (5, 6543),
        ):
            with self.subTest(index=index):
                row = original.copy()
                row[index] = value
                self.cluster = tuple(row)
                with self.assertRaisesRegex(NativePostgresError, "configured local"):
                    self.prepare()
                self.assertFalse(self.created)

    def test_reject_unowned_postmaster_before_creating(self):
        (self.data / "postmaster.pid").write_text(f"777\n{self.data}\n1000\n5432\n")
        with self.assertRaisesRegex(NativePostgresError, "managed process"):
            self.prepare()
        self.assertFalse(self.created)

    def test_managed_shell_wrapper_may_own_postmaster_child(self):
        self.process.pid = 122
        with patch(
            "utils.infinidysk_v2_postgres_prepare.psutil.Process",
            side_effect=lambda pid: SimpleNamespace(
                pid=pid,
                children=lambda **kwargs: [SimpleNamespace(pid=123)],
                ppid=lambda: 123,
            ),
        ):
            self.assertTrue(self.prepare()["created"])

    def test_server_backend_must_belong_to_owned_postmaster(self):
        with patch(
            "utils.infinidysk_v2_postgres_prepare.psutil.Process",
            side_effect=lambda pid: SimpleNamespace(
                pid=pid, children=lambda **kwargs: [], ppid=lambda: 777
            ),
        ):
            with self.assertRaisesRegex(NativePostgresError, "managed process"):
                self.prepare()
        self.assertFalse(self.created)

    def test_no_create_permission_does_not_mutate_roles(self):
        self.can_create = False
        with self.assertRaisesRegex(NativePostgresError, "permission to create"):
            self.prepare()
        self.assertFalse(self.created)
        self.assertFalse(
            any(isinstance(query, sql.Composed) for query, _ in self.calls)
        )

    def test_busy_advisory_lock_does_not_create(self):
        self.lock = False
        with self.assertRaisesRegex(
            NativePostgresError, "Another database preparation"
        ):
            self.prepare()
        self.assertFalse(self.created)

    def test_failed_final_identity_check_retains_new_empty_database(self):
        self.identity["system_identifier"] = "different"
        with self.assertRaisesRegex(NativePostgresError, "retained it"):
            self.prepare()
        self.assertTrue(self.created)
        self.assertIsNotNone(self.existing)

    def test_driver_error_does_not_expose_private_credentials(self):
        with self.assertRaises(NativePostgresError) as raised:
            prepare_managed_target(
                self.manager,
                self.options,
                connect=Mock(
                    side_effect=psycopg2.OperationalError("fixture-only-secret")
                ),
            )
        self.assertNotIn("fixture-only-secret", str(raised.exception))

    def test_interrupted_native_update_blocks_preparation(self):
        with patch(
            "utils.infinidysk_v2_update.native_update_state",
            return_value={"recovery_required": True},
        ):
            with self.assertRaisesRegex(NativePostgresError, "runtime update recovery"):
                self.prepare()
        self.assertEqual(self.connections, [])

    def test_manager_does_not_prepare_while_native_state_blocks_copy(self):
        self.config.config["infinidysk"]["runtime_generation"] = "2"
        manager = InfiniDyskV2PostgresManager(config_manager=self.config)
        with (
            patch.object(manager, "status", return_value={"actions": {"start": False}}),
            patch(
                "utils.infinidysk_v2_postgres_prepare.prepare_managed_target"
            ) as prepare,
        ):
            with self.assertRaisesRegex(NativePostgresError, "not currently available"):
                manager.prepare_target(self.options)
        prepare.assert_not_called()

    def test_preview_never_provisions_managed_database(self):
        self.config.config["infinidysk"]["runtime_generation"] = "2"
        manager = InfiniDyskV2PostgresManager(
            config_manager=self.config,
            target_inspector=lambda *args, **kwargs: copy.deepcopy(self.identity),
        )
        with (
            patch.object(manager, "status", return_value={"actions": {"start": True}}),
            patch.object(manager, "_native", return_value={}),
            patch(
                "utils.infinidysk_v2_postgres_prepare.prepare_managed_target"
            ) as prepare,
        ):
            self.assertTrue(manager.preview(self.options)["ready"])
        prepare.assert_not_called()
