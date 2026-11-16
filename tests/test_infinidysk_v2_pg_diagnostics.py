"""PostgreSQL snapshot transport and authored failure diagnostics."""

import ipaddress
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_infinidysk_v2_pg_snapshot as fixtures
from utils import infinidysk_v2_pg_snapshot as snapshot
from utils.infinidysk_v2_subprocess import MigrationProcessError


class PostgresSnapshotDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PostgresSnapshotTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def assert_tcp_address(self, address):
        self.fixture.data["server_address"] = address
        self.fixture.bind()
        receipt = self.fixture.snapshot()
        environment = self.fixture.commands[-1][1]["env"]
        self.assertEqual(str(ipaddress.ip_address(environment["PGHOSTADDR"])), address)
        self.assertNotIn("/", environment["PGHOSTADDR"])
        self.assertEqual(environment["PGHOST"], "source.example")
        self.assertEqual(environment["PGSSLMODE"], "verify-full")
        self.assertEqual(receipt["source_binding"]["server"]["address"], address)
        self.assertTrue(receipt["verified_restore"])
        self.assertTrue(
            any(
                "pg_catalog.host(inet_server_addr())" in query
                for query, _ in self.fixture.connections[-1].queries
            )
        )

    def test_ipv4_address_pinning_uses_host_not_inet_text_and_keeps_tls_hostname(self):
        self.assert_tcp_address("192.0.2.10")

    def test_ipv6_address_pinning_uses_host_not_cidr_and_keeps_tls_hostname(self):
        self.assert_tcp_address("2001:db8::10")

    def test_unix_source_keeps_configured_socket_and_omits_hostaddr(self):
        self.fixture.data["server_address"] = None
        service = self.fixture.plan["source"]["service"]
        service["env"]["DATABASE_CONNECTION_STRING"] = service["env"][
            "DATABASE_CONNECTION_STRING"
        ].replace("Host=source.example", "Host=/tmp/example-pg-socket")
        self.fixture.bind()
        self.fixture.snapshot()
        environment = self.fixture.commands[-1][1]["env"]
        self.assertNotIn("PGHOSTADDR", environment)
        self.assertEqual(environment["PGHOST"], "/tmp/example-pg-socket")
        self.assertEqual(environment["PGSSLMODE"], "verify-full")

    def test_production_monitored_dump_failure_identifies_dump_not_restore(self):
        with (
            patch.object(snapshot.subprocess, "run", side_effect=self.fixture.runner),
            patch.object(
                snapshot,
                "run_migration_process",
                side_effect=MigrationProcessError(
                    "private;source-secret untrusted stderr row"
                ),
            ),
            self.assertLogs(snapshot.__name__, level="ERROR") as logs,
        ):
            with self.assertRaises(
                snapshot.InfiniDyskV2PostgresSnapshotError
            ) as caught:
                snapshot.create_postgres_snapshot(
                    self.fixture.plan,
                    {"root": str(self.fixture.root / "job")},
                    connect=self.fixture.connect,
                    verify_restore=self.fixture.restore,
                    pg_dump="/usr/bin/pg_dump",
                )
        self.assertIn("running pg_dump", str(caught.exception))
        self.assertNotIn("isolated restore verification failed", str(caught.exception))
        self.assertNotIn("source-secret", str(caught.exception) + " ".join(logs.output))
        self.assertIn("database_dump", " ".join(logs.output))
        self.assertTrue((self.fixture.root / "job/v1-postgres-import.sqlite").is_file())
        self.assertFalse((self.fixture.root / "job/v1-postgres.partial").exists())
        self.assertFalse(self.fixture.restore_calls)
        self.assertTrue(self.fixture.connections[-1].closed)

    def test_sqlite_space_failure_is_actionable_and_never_exposes_row_values(self):
        failure = sqlite3.OperationalError("private;source-secret row contents")
        failure.sqlite_errorcode = sqlite3.SQLITE_FULL
        with (
            patch.object(snapshot, "_stage_legacy", side_effect=failure),
            self.assertLogs(snapshot.__name__, level="ERROR") as logs,
        ):
            with self.assertRaises(
                snapshot.InfiniDyskV2PostgresSnapshotError
            ) as caught:
                self.fixture.snapshot()
        self.assertIn("creating the local SQLite copy", str(caught.exception))
        self.assertIn("no available space", str(caught.exception))
        self.assertNotIn("source-secret", str(caught.exception) + " ".join(logs.output))
        self.assertFalse(self.fixture.restore_calls)

    def test_authentication_failure_uses_sqlstate_without_driver_message(self):
        class Failure(RuntimeError):
            pgcode = "28P01"

        def connect(**settings):
            raise Failure("Host=private.example;Password=source-secret")

        with self.assertLogs(snapshot.__name__, level="ERROR") as logs:
            with self.assertRaises(
                snapshot.InfiniDyskV2PostgresSnapshotError
            ) as caught:
                snapshot.create_postgres_snapshot(
                    self.fixture.plan,
                    {"root": str(self.fixture.root / "job")},
                    connect=connect,
                    run=self.fixture.runner,
                    verify_restore=self.fixture.restore,
                    pg_dump="/usr/bin/pg_dump",
                )
        self.assertIn("opening the read-only", str(caught.exception))
        self.assertIn("rejected authentication", str(caught.exception))
        self.assertNotIn("source-secret", str(caught.exception) + " ".join(logs.output))
        self.assertNotIn("private.example", " ".join(logs.output))

    def test_restore_failure_is_distinguished_from_source_copy_and_dump(self):
        def restore(*args):
            raise RuntimeError("private;source-secret restored data")

        with self.assertLogs(snapshot.__name__, level="ERROR") as logs:
            with self.assertRaises(
                snapshot.InfiniDyskV2PostgresSnapshotError
            ) as caught:
                snapshot.create_postgres_snapshot(
                    self.fixture.plan,
                    {"root": str(self.fixture.root / "job")},
                    connect=self.fixture.connect,
                    run=self.fixture.runner,
                    verify_restore=restore,
                    pg_dump="/usr/bin/pg_dump",
                )
        self.assertIn("restoring and validating the independent", str(caught.exception))
        self.assertIn("restore_verification", " ".join(logs.output))
        self.assertNotIn("source-secret", str(caught.exception) + " ".join(logs.output))
        self.assertTrue((self.fixture.root / "job/v1-postgres.dump").is_file())

    def test_progress_separates_full_inventory_from_seven_table_staging_and_dump(self):
        events = []
        self.fixture.snapshot(
            progress=lambda stage, message, **detail: events.append(
                (stage, message, detail)
            )
        )
        inventory = [
            d for _, _, d in events if str(d.get("table", "")).startswith("public.")
        ]
        self.assertTrue(inventory)
        self.assertTrue(all(d["table_count"] == 8 for d in inventory))
        accounts = [d for d in inventory if d["table"] == "public.Accounts"]
        self.assertEqual(accounts[0]["rows"], 0)
        self.assertEqual(accounts[-1]["rows"], 1)
        staged = [d for _, _, d in events if d.get("table") in snapshot.LEGACY_TABLES]
        self.assertTrue(staged)
        self.assertTrue(all(d["table_count"] == 7 for d in staged))
        for index, name in enumerate(snapshot.LEGACY_TABLES, 1):
            rows = [d for d in staged if d["table"] == name]
            self.assertEqual(rows[0]["rows"], 0)
            self.assertTrue(all(d["table_index"] == index for d in rows))
            self.assertGreaterEqual(len(rows), 2)
        dump = next(d for stage, _, d in events if stage == "postgres_dump")
        self.assertIsNone(dump["table"])
        self.assertEqual(
            (dump["table_index"], dump["table_count"], dump["rows"]), (0, 0, 0)
        )


if __name__ == "__main__":
    unittest.main()
