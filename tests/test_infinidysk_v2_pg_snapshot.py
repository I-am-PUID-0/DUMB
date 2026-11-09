import copy
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from utils.infinidysk_v2_pg_snapshot import (
    InfiniDyskV2PostgresSnapshotError,
    LEGACY_TABLES,
    connection_settings,
    create_postgres_snapshot,
    inspect_postgres_source,
    verify_restored_connection,
)


def fixture():
    history = "20260824160000_Add-Health-Repair-Pending"
    tables = {}
    for name in LEGACY_TABLES:
        tables[f"public.{name}"] = {
            "columns": [("Id", "uuid", True, None, "")],
            "rows": [],
        }
    tables["public.__EFMigrationsHistory"] = {
        "columns": [
            ("MigrationId", "character varying(150)", True, None, ""),
            ("ProductVersion", "character varying(32)", True, None, ""),
        ],
        "rows": [(history, "9.0.0")],
    }
    tables["public.Accounts"] = {
        "columns": [
            ("Id", "uuid", True, None, ""),
            ("Enabled", "boolean", True, None, ""),
            ("Count", "bigint", False, None, ""),
            ("Created", "timestamp with time zone", True, None, ""),
            ("Optional", "text", False, None, ""),
        ],
        "rows": [
            (
                "00000000-0000-0000-0000-0000000000ab",
                True,
                2**50,
                "2026-11-09 11:00:00+00",
                None,
            ),
        ],
    }
    # Full backup verification must cover tables omitted from native staging.
    tables["public.AuditOnly"] = {
        "columns": [("Message", "text", True, None, "")],
        "rows": [("must survive the full backup",)],
    }
    return {
        "tables": tables,
        "system": "123456789",
        "database": "16384",
        "timezone": "UTC",
        "database_characteristics": ["UTF8", "C", "C"],
        "constraints": [
            ["public", "Accounts", "Accounts_pkey", "p", True, 'PRIMARY KEY ("Id")']
        ],
        "indexes": [
            [
                "public",
                "Accounts",
                "Accounts_pkey",
                'CREATE UNIQUE INDEX "Accounts_pkey" ON public."Accounts" USING btree ("Id")',
            ]
        ],
        "triggers": [],
        "sequences": [
            ["public", "ids", "bigint", 1, 1, 9223372036854775807, 1, False, 1]
        ],
        "sequence_state": {"public.ids": ["73", True]},
    }


class Cursor:
    def __init__(self, connection, name=None):
        self.connection = connection
        self.name = name
        self.results = []
        self.itersize = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, sql, values=None):
        self.connection.queries.append((sql, values))
        data = self.connection.data
        if sql.startswith("SET LOCAL"):
            self.results = []
        elif "set_config('TimeZone'" in sql:
            self.results = [(values[0],)]
        elif "transaction_read_only" in sql:
            self.results = [("on" if self.connection.readonly else "off",)]
        elif sql == "SHOW TimeZone":
            self.results = [(data["timezone"],)]
        elif "SELECT pg_encoding_to_char(encoding)" in sql:
            self.results = [data["database_characteristics"]]
        elif sql == "SELECT pg_database_size(current_database())":
            self.results = [(8 * 1024 * 1024,)]
        elif "FROM pg_class c JOIN pg_namespace" in sql:
            self.results = [tuple(key.split(".", 1)) for key in sorted(data["tables"])]
        elif "FROM pg_attribute" in sql:
            self.results = data["tables"][".".join(values)]["columns"]
        elif (
            sql
            == 'SELECT "MigrationId" FROM public."__EFMigrationsHistory" ORDER BY "MigrationId"'
        ):
            self.results = [
                (row[0],)
                for row in data["tables"]["public.__EFMigrationsHistory"]["rows"]
            ]
        elif "FROM pg_constraint" in sql:
            self.results = data["constraints"]
        elif "FROM pg_indexes" in sql:
            self.results = data["indexes"]
        elif "FROM pg_trigger" in sql:
            self.results = data["triggers"]
        elif "FROM pg_sequences" in sql:
            self.results = data["sequences"]
        elif sql.startswith("SELECT last_value::text,is_called FROM "):
            match = re.search(r'FROM "([^"]+)"."([^"]+)"$', sql)
            self.results = [data["sequence_state"][".".join(match.groups())]]
        elif "pg_control_system()" in sql:
            self.results = [(data["system"],)]
        elif "FROM pg_database" in sql:
            self.results = [(data["database"],)]
        elif "inet_server_addr()" in sql:
            self.results = [("192.0.2.10", 5432)]
        elif "server_version_num" in sql:
            self.results = [(160015,)]
        elif "pg_export_snapshot()" in sql:
            self.results = [("00000003-00000019-1",)]
            self.connection.exported = True
        elif self.name:
            match = re.search(r' FROM (?:"public"|public)."([^"]+)"$', sql)
            if not match:
                raise AssertionError(f"Unexpected cursor query: {sql}")
            table = data["tables"]["public." + match.group(1)]
            expressions = sql.removeprefix("SELECT ").split(" FROM ")[0].split(",")
            converted = []
            for row in table["rows"]:
                result = []
                for value, expression in zip(row, expressions):
                    if value is None:
                        result.append(None)
                    elif "upper(" in expression:
                        result.append(str(value).upper())
                    elif "::int" in expression:
                        result.append(int(value))
                    else:
                        result.append(
                            "true"
                            if value is True
                            else "false" if value is False else str(value)
                        )
                converted.append(tuple(result))
            self.results = converted
        else:
            raise AssertionError(f"Unexpected query: {sql}")

    def fetchone(self):
        return self.results[0]

    def fetchall(self):
        return self.results

    def __iter__(self):
        return iter(self.results)


class Connection:
    def __init__(self, data):
        self.data = copy.deepcopy(data)
        self.queries = []
        self.readonly = False
        self.sessions = []
        self.rolled_back = False
        self.closed = False
        self.exported = False

    def cursor(self, name=None):
        return Cursor(self, name)

    def set_session(self, **kwargs):
        self.sessions.append(kwargs)
        self.readonly = kwargs["readonly"]

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class PostgresSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = fixture()
        self.plan = {
            "source": {
                "service": {
                    "env": {
                        "DATABASE_CONNECTION_STRING": (
                            "Host=source.example;Port=5432;Database=app;Username=app;"
                            "Password='private;source-secret';SSL Mode=VerifyFull;"
                            "Root Certificate=/private/ca.pem"
                        )
                    }
                }
            }
        }
        self.connections = []
        self.commands = []
        self.restore_calls = []
        self.bind()

    def tearDown(self):
        self.temp.cleanup()

    def connect(self, **kwargs):
        connection = Connection(self.data)
        self.connections.append(connection)
        return connection

    def bind(self):
        self.plan["source"]["postgres_binding"] = inspect_postgres_source(
            self.plan, connect=self.connect
        )

    def runner(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command[-1] == "--version":
            return subprocess.CompletedProcess(
                command, 0, stdout=b"pg_dump (PostgreSQL) 16.15\n"
            )
        self.assertTrue(self.connections[-1].exported)
        self.assertFalse(self.connections[-1].closed)
        self.assertEqual(
            command[command.index("--snapshot") + 1], "00000003-00000019-1"
        )
        Path(command[command.index("--file") + 1]).write_bytes(
            b"independent custom dump"
        )
        return subprocess.CompletedProcess(command, 0)

    def restore(self, dump, binding, settings):
        self.assertTrue(self.connections[-1].closed)
        self.assertTrue(Path(dump).is_file())
        self.assertEqual(settings["sslmode"], "verify-full")
        self.restore_calls.append((dump, binding, settings))
        restored = copy.deepcopy(self.data)
        restored["system"] = "987654321"
        return verify_restored_connection(Connection(restored), binding)

    def snapshot(self, **kwargs):
        return create_postgres_snapshot(
            self.plan,
            {"root": str(self.root / "job")},
            connect=self.connect,
            run=self.runner,
            verify_restore=self.restore,
            pg_dump="/usr/bin/pg_dump",
            **kwargs,
        )

    def test_one_readonly_snapshot_drives_full_dump_and_exact_native_codec(self):
        receipt = self.snapshot()
        self.assertTrue(receipt["verified_restore"])
        self.assertTrue(receipt["source_read_only"])
        self.assertEqual(receipt["physical_identity"]["system_identifier"], "123456789")
        connection = self.connections[-1]
        self.assertEqual(
            connection.sessions,
            [
                {
                    "isolation_level": "REPEATABLE READ",
                    "readonly": True,
                    "autocommit": False,
                }
            ],
        )
        self.assertTrue(connection.closed)
        self.assertTrue(connection.rolled_back)
        self.assertEqual(len(self.restore_calls), 1)
        self.assertIn("public.AuditOnly", receipt["source_binding"]["table_digests"])
        with sqlite3.connect(receipt["staged_sqlite"]) as db:
            names = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual(names, set(LEGACY_TABLES))
            self.assertEqual(
                db.execute("SELECT * FROM Accounts").fetchone(),
                (
                    "00000000-0000-0000-0000-0000000000AB",
                    1,
                    2**50,
                    "2026-11-09 11:00:00+00",
                    None,
                ),
            )
        for path in (receipt["dump_path"], receipt["staged_sqlite"]):
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
        serialized = json.dumps(receipt)
        self.assertNotIn("private;source-secret", serialized)
        for command, kwargs in self.commands:
            self.assertNotIn("private;source-secret", " ".join(command))
            self.assertEqual(kwargs["env"]["PGSSLMODE"], "verify-full")
        self.assertEqual(self.commands[-1][1]["env"]["PGHOSTADDR"], "192.0.2.10")

    def test_restore_rejects_corruption_in_non_imported_table(self):
        def corrupt(dump, binding, settings):
            damaged = copy.deepcopy(self.data)
            damaged["system"] = "987654321"
            damaged["tables"]["public.AuditOnly"]["rows"] = [("corrupt",)]
            return verify_restored_connection(Connection(damaged), binding)

        with self.assertRaisesRegex(InfiniDyskV2PostgresSnapshotError, "differs"):
            self.snapshot_with_verifier(corrupt)

    def snapshot_with_verifier(self, verifier):
        return create_postgres_snapshot(
            self.plan,
            {"root": str(self.root / "job")},
            connect=self.connect,
            run=self.runner,
            verify_restore=verifier,
            pg_dump="/usr/bin/pg_dump",
        )

    def test_restore_rejects_changed_constraint_or_sequence_state(self):
        for mutate in (
            "constraint",
            "sequence",
            "column_collation",
            "database_collation",
        ):
            with self.subTest(mutate=mutate):
                data = copy.deepcopy(self.data)
                data["system"] = "987654321"
                binding = inspect_postgres_source(
                    self.plan, connect=lambda **_: Connection(data)
                )
                # Obtain full source binding via the normal execution path.
                receipt = self.snapshot()
                source = receipt["source_binding"]
                if mutate == "constraint":
                    data["constraints"][0][-1] = 'PRIMARY KEY ("Other")'
                elif mutate == "column_collation":
                    column = data["tables"]["public.Accounts"]["columns"][3]
                    data["tables"]["public.Accounts"]["columns"][3] = (
                        *column,
                        "public.foreign_collation",
                    )
                elif mutate == "database_collation":
                    data["database_characteristics"][1] = "foreign"
                else:
                    data["sequence_state"]["public.ids"][0] = "74"
                with self.assertRaisesRegex(
                    InfiniDyskV2PostgresSnapshotError, "differs"
                ):
                    verify_restored_connection(Connection(data), source)
                self.assertTrue(binding["schema_fingerprint"])
                for path in (self.root / "job").iterdir():
                    path.unlink()
                (self.root / "job").rmdir()

    def test_restore_verifier_cannot_point_at_the_original_database(self):
        with self.assertRaisesRegex(InfiniDyskV2PostgresSnapshotError, "independent"):
            self.snapshot_with_verifier(
                lambda dump, binding, settings: verify_restored_connection(
                    Connection(self.data), binding
                )
            )

    def test_connection_failures_are_redacted_before_a_transaction_exists(self):
        def unavailable(**settings):
            raise RuntimeError("private;source-secret driver error")

        with self.assertRaises(InfiniDyskV2PostgresSnapshotError) as error:
            inspect_postgres_source(self.plan, connect=unavailable)
        self.assertNotIn("source-secret", str(error.exception))

    def test_preview_physical_or_schema_drift_stops_before_dump(self):
        for field in ("system", "database", "schema"):
            with self.subTest(field=field):
                original = copy.deepcopy(self.data)
                if field == "schema":
                    self.data["tables"]["public.Accounts"]["columns"][0] = (
                        "Id",
                        "text",
                        True,
                        None,
                        "",
                    )
                else:
                    self.data[field] += "9"
                with self.assertRaisesRegex(
                    InfiniDyskV2PostgresSnapshotError, "changed"
                ):
                    self.snapshot()
                self.assertFalse(self.commands)
                self.data = original

    def test_capacity_refusal_happens_before_staging_or_dump_writes(self):
        with patch(
            "utils.infinidysk_v2_pg_snapshot.shutil.disk_usage",
            return_value=SimpleNamespace(free=1),
        ):
            with self.assertRaisesRegex(
                InfiniDyskV2PostgresSnapshotError, "free space"
            ):
                self.snapshot()
        self.assertFalse(self.commands)
        self.assertFalse((self.root / "job" / "v1-postgres-import.sqlite").exists())
        self.assertFalse((self.root / "job" / "v1-postgres.dump").exists())

    def test_no_restore_verifier_cannot_accept_backup(self):
        with self.assertRaisesRegex(InfiniDyskV2PostgresSnapshotError, "verifier"):
            create_postgres_snapshot(
                self.plan,
                {"root": str(self.root / "job")},
                connect=self.connect,
                run=self.runner,
            )
        self.assertFalse(self.commands)

    def test_invented_restore_success_cannot_pass_binding_check(self):
        with self.assertRaisesRegex(InfiniDyskV2PostgresSnapshotError, "matching"):
            self.snapshot_with_verifier(
                lambda *_: {"verified": True, "source_binding": "forged"}
            )

    def test_connection_policy_preserves_quoted_password_and_tls(self):
        settings = connection_settings(self.plan)
        self.assertEqual(settings["password"], "private;source-secret")
        self.assertEqual(settings["sslmode"], "verify-full")
        self.assertEqual(settings["sslrootcert"], "/private/ca.pem")
        self.assertIn("default_transaction_read_only=on", settings["options"])
        self.assertIn("search_path=public", settings["options"])

    def test_connection_parser_never_discards_security_or_schema_options(self):
        for suffix in (
            ";SSL Mode=unexpected",
            ";Search Path=foreign",
            ";Kerberos Service Name=custom",
            ";Trust Server Certificate=true",
            ";Host=first.example,second.example",
            ";SSL Mode='VerifyFull'ignored",
            ";Password='unterminated",
        ):
            with self.subTest(suffix=suffix):
                plan = copy.deepcopy(self.plan)
                plan["source"]["service"]["env"]["DATABASE_CONNECTION_STRING"] += suffix
                with self.assertRaises(InfiniDyskV2PostgresSnapshotError):
                    connection_settings(plan)

    def test_native_floor_and_missing_import_table_are_rejected(self):
        self.data["tables"]["public.__EFMigrationsHistory"]["rows"] = [
            ("20250101000000_OldSchema", "9.0")
        ]
        with self.assertRaisesRegex(InfiniDyskV2PostgresSnapshotError, "v1.3"):
            self.bind()
        self.data = fixture()
        del self.data["tables"]["public.DavItems"]
        self.bind()
        with self.assertRaisesRegex(InfiniDyskV2PostgresSnapshotError, "missing"):
            self.snapshot()

    def test_dump_failure_is_redacted_and_source_transaction_is_closed(self):
        def fail(command, **kwargs):
            if command[-1] == "--version":
                return self.runner(command, **kwargs)
            raise RuntimeError("private;source-secret leaked driver detail")

        with self.assertRaises(InfiniDyskV2PostgresSnapshotError) as error:
            create_postgres_snapshot(
                self.plan,
                {"root": str(self.root / "job")},
                connect=self.connect,
                run=fail,
                verify_restore=self.restore,
                pg_dump="/usr/bin/pg_dump",
            )
        self.assertNotIn("source-secret", str(error.exception))
        self.assertTrue(self.connections[-1].closed)
        self.assertTrue(self.connections[-1].rolled_back)
        self.assertFalse((self.root / "job" / "v1-postgres.partial").exists())

    def test_dump_major_must_match_server_and_no_source_write_is_used(self):
        def wrong_version(command, **kwargs):
            return subprocess.CompletedProcess(
                command, 0, stdout=b"pg_dump (PostgreSQL) 15.8\n"
            )

        with self.assertRaisesRegex(InfiniDyskV2PostgresSnapshotError, "major"):
            create_postgres_snapshot(
                self.plan,
                {"root": str(self.root / "job")},
                connect=self.connect,
                run=wrong_version,
                verify_restore=self.restore,
                pg_dump="/usr/bin/pg_dump",
            )
        for connection in self.connections:
            for sql, _ in connection.queries:
                self.assertTrue(sql.startswith(("SELECT ", "SHOW ", "SET LOCAL ")), sql)

    def test_existing_artifacts_and_symlink_job_storage_are_refused(self):
        job = self.root / "job"
        job.mkdir()
        (job / "v1-postgres.dump").symlink_to(self.root / "outside")
        with self.assertRaisesRegex(InfiniDyskV2PostgresSnapshotError, "occupies"):
            self.snapshot()
        link = self.root / "alias"
        link.symlink_to(job, target_is_directory=True)
        with self.assertRaisesRegex(InfiniDyskV2PostgresSnapshotError, "symbolic"):
            create_postgres_snapshot(
                self.plan,
                {"root": str(link)},
                verify_restore=self.restore,
                connect=self.connect,
                run=self.runner,
                pg_dump="/usr/bin/pg_dump",
            )


if __name__ == "__main__":
    unittest.main()
