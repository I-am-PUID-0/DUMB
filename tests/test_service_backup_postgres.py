"""Selected-service DB ownership and disposable-cluster restore qualification."""

import copy
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import psycopg2
from psycopg2 import sql

from utils.service_backup_postgres import (
    PostgresServiceBackup,
    ServicePostgresError,
    _connection_target,
    _database_name,
    _file_proof,
    _private_directory,
    service_databases,
)


def configuration():
    return {
        "postgres": {
            "enabled": True,
            "process_name": "PostgreSQL",
            "host": "127.0.0.1",
            "port": 5432,
            "user": "DUMB",
            "password": "postgres",
            "config_dir": "/postgres_data",
        },
        "radarr": {
            "instances": {
                "Default": {
                    "enabled": True,
                    "process_name": "Radarr",
                    "postgres_enabled": True,
                }
            }
        },
    }


class ServiceDatabaseMappingTests(unittest.TestCase):
    def test_arr_maps_main_and_log_without_cluster_database(self):
        self.assertEqual(
            service_databases(configuration(), "radarr", "Default"),
            [
                {"database": "radarr-main", "role": "main"},
                {"database": "radarr-log", "role": "logs"},
            ],
        )

    def test_disabled_instance_still_owns_its_database(self):
        config = configuration()
        config["radarr"]["instances"]["Other"] = {
            "enabled": False,
            "process_name": "Other Radarr",
            "postgres_enabled": True,
            "postgres_main_db": "radarr-main",
            "postgres_log_db": "other-log",
        }
        with self.assertRaisesRegex(ServicePostgresError, "shared"):
            service_databases(config, "radarr", "Default")

    def test_main_and_log_cannot_alias_the_same_database(self):
        config = configuration()
        config["radarr"]["instances"]["Default"].update(
            postgres_main_db="shared", postgres_log_db="shared"
        )
        with self.assertRaisesRegex(ServicePostgresError, "shared"):
            service_databases(config, "radarr", "Default")

    def test_unconfigured_non_arr_instance_uses_declared_database(self):
        config = configuration()
        config["seerr"] = {
            "instances": {
                "Movies": {
                    "process_name": "Seerr Movies",
                    "postgres_enabled": True,
                    "postgres_database": "seerr_movies",
                }
            }
        }
        self.assertEqual(
            service_databases(config, "seerr", "Movies"),
            [{"database": "seerr_movies", "role": "main"}],
        )

    def test_sqlite_does_not_require_managed_postgres(self):
        config = configuration()
        config["postgres"]["enabled"] = False
        config["radarr"]["instances"]["Default"]["postgres_enabled"] = False
        config["seerr"] = {
            "enabled": False,
            "process_name": "Retained Seerr",
            "postgres_enabled": True,
        }
        self.assertEqual(service_databases(config, "radarr", "Default"), [])

    def test_external_selected_database_is_not_silently_omitted(self):
        config = configuration()
        config["seerr"] = {
            "process_name": "Seerr",
            "postgres_enabled": True,
            "env": {"DB_TYPE": "postgres", "DB_HOST": "database.example.invalid"},
        }
        with self.assertRaisesRegex(ServicePostgresError, "external"):
            service_databases(config, "seerr")

    def test_separate_credentials_do_not_hide_shared_local_database(self):
        config = configuration()
        config["seerr"] = {
            "process_name": "Seerr",
            "postgres_enabled": True,
            "env": {
                "DB_TYPE": "postgres",
                "DB_HOST": "127.0.0.1",
                "DB_USER": "another_role",
                "DB_NAME": "radarr-main",
            },
        }
        with self.assertRaisesRegex(ServicePostgresError, "shared"):
            service_databases(config, "radarr", "Default")

    def test_conflicting_arr_runtime_names_are_rejected(self):
        config = configuration()
        with patch(
            "utils.service_backup_postgres.read_arr_postgres_xml_values",
            return_value={
                "PostgresMainDb": "somewhere_else",
                "PostgresLogDb": "radarr-log",
            },
        ):
            with self.assertRaisesRegex(ServicePostgresError, "names differ"):
                service_databases(config, "radarr", "Default")

    def test_runtime_url_overrides_and_extra_credentials_rejected(self):
        managed = PostgresServiceBackup(configuration()).params
        for connection in (
            {"dsn": "postgres://DUMB:postgres@127.0.0.1/db?host=other.invalid"},
            {"dsn": "postgres://DUMB:postgres@127.0.0.1:5433/db"},
            {"host": "127.0.0.1", "user": "another_role"},
            {"host": "127.0.0.1", "password": "different"},
            {"host": "::1", "database": "db"},
        ):
            with self.subTest(connection=tuple(connection)):
                with self.assertRaises(ServicePostgresError):
                    _connection_target(connection, managed)

    def test_system_and_transaction_names_rejected(self):
        for name in (
            "postgres",
            "template0",
            "template1",
            "dumb_sb_owned",
            "a/b",
            "a;drop",
            "x" * 64,
        ):
            with self.subTest(name=name):
                with self.assertRaises(ServicePostgresError):
                    _database_name(name)

    def test_missing_instance_not_treated_as_empty_database_set(self):
        with self.assertRaisesRegex(ServicePostgresError, "no longer exists"):
            service_databases(configuration(), "radarr", "Missing")

    def test_fixed_database_mapping(self):
        config = configuration()
        config["riven_backend"] = {"process_name": "Riven"}
        self.assertEqual(
            service_databases(config, "riven_backend"),
            [{"database": "riven", "role": "main"}],
        )

    def test_legacy_infinidysk_pg_requires_installation_restore(self):
        config = configuration()
        config["infinidysk"] = {"process_name": "InfiniDysk", "postgres_enabled": True}
        with self.assertRaisesRegex(ServicePostgresError, "whole-installation"):
            service_databases(config, "infinidysk")


class ServiceDatabaseArtifactTests(unittest.TestCase):
    def test_cancel_callback_stops_native_helper_before_return(self):
        class Cancelled(Exception):
            pass

        class Child:
            pid = 424242
            returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = -15
                return self.returncode

        def progress(value):
            raise Cancelled

        adapter = PostgresServiceBackup(configuration())
        child = Child()
        with (
            patch.object(adapter, "_tool", return_value="/test/pg_dump"),
            patch("utils.service_backup_postgres.subprocess.Popen", return_value=child),
            patch("utils.service_backup_postgres.os.killpg") as terminate,
        ):
            with self.assertRaises(Cancelled):
                adapter._command(
                    "pg_dump", 16, "radarr-main", ["--format=custom"], progress=progress
                )
        self.assertIsNotNone(child.returncode)
        terminate.assert_called_once()

    def test_checksum_private_file_and_reject_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / "000.dump"
            path.write_bytes(b"dump bytes")
            path.chmod(0o600)
            self.assertEqual(
                _file_proof(path),
                {"bytes": 10, "sha256": hashlib.sha256(b"dump bytes").hexdigest()},
            )
            linked = directory / "alias.dump"
            linked.symlink_to(path)
            with self.assertRaises(OSError):
                _file_proof(linked)
            linked.unlink()
            os.link(path, linked)
            with self.assertRaisesRegex(ServicePostgresError, "regular file"):
                _file_proof(path)

    def test_shared_directory_is_not_chmodded_or_adopted(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "shared"
            directory.mkdir(mode=0o755)
            with self.assertRaisesRegex(ServicePostgresError, "controller-private"):
                _private_directory(directory)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o755)

    def test_helper_credentials_are_not_arguments_or_error_details(self):
        calls = []

        def run(arguments, **options):
            calls.append((arguments, options))
            return subprocess.CompletedProcess(arguments, 1)

        adapter = PostgresServiceBackup(configuration(), run=run)
        with (
            patch.object(adapter, "_tool", return_value="/test/pg_dump"),
            patch.dict(os.environ, {"PGOPTIONS": "unsafe", "PGSERVICE": "other"}),
        ):
            with self.assertRaises(ServicePostgresError) as raised:
                adapter._command("pg_dump", 16, "radarr-main", ["--format=custom"])
        self.assertNotIn(
            "postgres", str(raised.exception).lower().replace("postgresql", "")
        )
        self.assertEqual(calls[0][0], ["/test/pg_dump", "--format=custom"])
        self.assertNotIn("PGOPTIONS", calls[0][1]["env"])
        self.assertNotIn("PGSERVICE", calls[0][1]["env"])
        self.assertEqual(calls[0][1]["env"]["PGDATABASE"], "radarr-main")
        self.assertEqual(calls[0][1]["stderr"], subprocess.DEVNULL)

    def test_empty_or_foreign_manifest_rejected_before_connecting(self):
        adapter = PostgresServiceBackup(configuration())
        with patch.object(adapter, "_locked") as locked:
            for manifest in ({}, {"format": "other", "version": 1, "databases": []}):
                with self.assertRaises(ServicePostgresError):
                    adapter.prepare_restore(
                        manifest, "/unused", "valid_job", lambda value: None
                    )
        locked.assert_not_called()


@unittest.skipUnless(
    os.environ.get("DUMB_SERVICE_BACKUP_PG_TESTS") == "1",
    "opt-in disposable PostgreSQL round trip",
)
class ServiceDatabaseRoundTripTests(unittest.TestCase):
    """Explicitly admitted isolated PG16 fixture, never the configured cluster."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tools = Path("/usr/lib/postgresql/16/bin")
        if os.geteuid() == 0 or not (cls.tools / "initdb").is_file():
            raise unittest.SkipTest("requires unprivileged PostgreSQL16 fixture tools")
        cls.temporary = tempfile.TemporaryDirectory(
            prefix="service-pg-", dir="/tmp/service-backups"
        )
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.data = cls.root / "cluster"
        cls.socket = cls.root / "socket"
        cls.socket.mkdir(mode=0o700)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            cls.port = reservation.getsockname()[1]
        cls.password = secrets.token_urlsafe(24)
        password_file = cls.root / "password"
        password_file.write_text(cls.password)
        password_file.chmod(0o600)
        subprocess.run(
            [
                str(cls.tools / "initdb"),
                "-D",
                str(cls.data),
                "-U",
                "backup_test_admin",
                "--auth=scram-sha-256",
                "--pwfile",
                str(password_file),
                "--no-locale",
                "--encoding=UTF8",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.addClassCleanup(cls._stop)
        subprocess.run(
            [
                str(cls.tools / "pg_ctl"),
                "-D",
                str(cls.data),
                "-l",
                str(cls.root / "server.log"),
                "-o",
                f"-h 127.0.0.1 -p {cls.port} -k {cls.socket}",
                "-w",
                "start",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.config = configuration()
        cls.config["postgres"].update(
            port=cls.port,
            user="backup_test_admin",
            password=cls.password,
            config_dir=str(cls.data),
        )
        cls.params = {
            "host": "127.0.0.1",
            "port": cls.port,
            "user": "backup_test_admin",
            "password": cls.password,
        }

    @classmethod
    def _stop(cls):
        if (cls.data / "postmaster.pid").exists():
            subprocess.run(
                [
                    str(cls.tools / "pg_ctl"),
                    "-D",
                    str(cls.data),
                    "-m",
                    "immediate",
                    "-w",
                    "stop",
                ],
                check=True,
                timeout=30,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        result = subprocess.run(
            [str(cls.tools / "pg_ctl"), "-D", str(cls.data), "status"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 3:
            raise AssertionError(
                "task-owned PostgreSQL child termination was not verified"
            )

    def setUp(self):
        self.adapter = PostgresServiceBackup(self.config)
        self.snapshots = []
        self.transactions = []
        self.work = self.root / secrets.token_hex(8)
        self.work.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.work)
        self.addCleanup(self._remove_databases)
        self._sql("postgres", "CREATE DATABASE selected_main OWNER backup_test_admin")
        self._sql("postgres", "CREATE DATABASE selected_log OWNER backup_test_admin")
        self._sql("postgres", "CREATE DATABASE unrelated OWNER backup_test_admin")
        for name in ("selected_main", "selected_log", "unrelated"):
            self._sql(
                name,
                "CREATE TABLE example (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, value text)",
            )
            self._sql(name, "INSERT INTO example(value) VALUES ('saved')")
        self.specs = [
            {"database": "selected_main", "role": "main"},
            {"database": "selected_log", "role": "logs"},
        ]

    def _sql(self, database, query, values=(), fetch=False):
        connection = psycopg2.connect(dbname=database, **self.params)
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(query, values)
                return cursor.fetchall() if fetch else None
        finally:
            connection.close()

    def _remove_databases(self):
        # Fixture cleanup is identity-scoped to this entirely disposable server.
        for (name,) in self._sql(
            "postgres",
            "SELECT datname FROM pg_database WHERE datname NOT IN ('postgres','template0','template1')",
            fetch=True,
        ):
            self._sql(
                "postgres",
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)),
            )
        for (name,) in self._sql(
            "postgres",
            "SELECT rolname FROM pg_roles WHERE rolname ~ '^dumb_sb_'",
            fetch=True,
        ):
            self._sql("postgres", sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))

    def _persist(self, transaction):
        self.snapshots.append(copy.deepcopy(transaction))

    def _prepare(self):
        manifest = self.adapter.backup(self.specs, self.work / "logical-postgres")
        self._sql("selected_main", "UPDATE example SET value='current'")
        self._sql("selected_log", "INSERT INTO example(value) VALUES ('current')")
        transaction = self.adapter.prepare_restore(
            manifest,
            self.work / "logical-postgres",
            "service_test_job",
            self._persist,
            specs=self.specs,
        )
        self.transactions.append(transaction)
        return transaction

    def test_round_trip_finish_and_repeat_finish_preserve_unrelated(self):
        self.assertGreater(self.adapter.estimate(self.specs), 0)
        transaction = self._prepare()
        self.assertTrue(self.adapter.verify_server_binding(transaction))
        self.assertEqual(
            self._sql("selected_main", "SELECT value FROM example", fetch=True),
            [("current",)],
        )
        self.adapter.apply_restore(transaction, self._persist)
        with self.assertRaises(psycopg2.OperationalError):
            psycopg2.connect(dbname="selected_main", **self.params)
        self.adapter.finish_restore(transaction, self._persist)
        self.adapter.finish_restore(transaction, self._persist)
        self.assertEqual(
            self._sql("selected_main", "SELECT value FROM example", fetch=True),
            [("saved",)],
        )
        self.assertEqual(
            self._sql("unrelated", "SELECT value FROM example", fetch=True),
            [("saved",)],
        )
        self.assertEqual(
            self._sql(
                "selected_main",
                "INSERT INTO example(value) VALUES ('next') RETURNING id",
                fetch=True,
            ),
            [(2,)],
        )
        self.assertEqual(
            self._sql(
                "postgres",
                "SELECT count(*) FROM pg_roles WHERE rolname ~ '^dumb_sb_'",
                fetch=True,
            ),
            [(0,)],
        )
        self.assertGreater(self.adapter.estimate(self.specs), 0)
        again = self.adapter.backup(self.specs, self.work / "after-restore")
        self.assertEqual(len(again["databases"]), 2)

    def test_apply_then_rollback_restores_original_current_data(self):
        transaction = self._prepare()
        self.adapter.apply_restore(transaction, self._persist)
        self.adapter.rollback_restore(transaction, self._persist)
        self.adapter.rollback_restore(transaction, self._persist)
        self.assertEqual(
            self._sql("selected_main", "SELECT value FROM example", fetch=True),
            [("current",)],
        )
        self.assertEqual(
            self._sql("selected_log", "SELECT count(*) FROM example", fetch=True),
            [(2,)],
        )
        self.assertEqual(
            self._sql(
                "postgres",
                "SELECT count(*) FROM pg_database WHERE datname ~ '^dumb_sb_'",
                fetch=True,
            ),
            [(0,)],
        )

    def test_lost_apply_acknowledgement_reconciles_actual_oids(self):
        transaction = self._prepare()

        def persist(value):
            if value["phase"] == "applied":
                raise OSError("simulated durable journal failure")
            self._persist(value)

        with self.assertRaises(OSError):
            self.adapter.apply_restore(transaction, persist)
        retained = copy.deepcopy(self.snapshots[-1])
        self.assertEqual(retained["phase"], "applying")
        self.adapter.rollback_restore(retained, self._persist)
        self.assertEqual(
            self._sql("selected_main", "SELECT value FROM example", fetch=True),
            [("current",)],
        )

    def test_active_database_client_is_not_terminated(self):
        transaction = self._prepare()
        client = psycopg2.connect(dbname="selected_main", **self.params)
        try:
            with self.assertRaisesRegex(ServicePostgresError, "active connections"):
                self.adapter.apply_restore(transaction, self._persist)
            with client.cursor() as cursor:
                cursor.execute("SELECT 1")
                self.assertEqual(cursor.fetchone(), (1,))
        finally:
            client.close()
        self.adapter.rollback_restore(transaction, self._persist)

    def test_changed_scratch_comment_blocks_cleanup_without_deletion(self):
        transaction = self._prepare()
        scratch = transaction["databases"][0]["scratch"]
        self._sql(
            "postgres",
            sql.SQL("COMMENT ON DATABASE {} IS 'foreign'").format(
                sql.Identifier(scratch)
            ),
        )
        with self.assertRaisesRegex(ServicePostgresError, "identity changed"):
            self.adapter.rollback_restore(transaction, self._persist)
        self.assertEqual(
            self._sql(
                "postgres",
                "SELECT count(*) FROM pg_database WHERE datname=%s",
                (scratch,),
                fetch=True,
            ),
            [(1,)],
        )

    def test_restore_sql_cannot_create_global_role(self):
        self._sql(
            "selected_main",
            "CREATE FUNCTION fixture_action() RETURNS int LANGUAGE plpgsql AS 'BEGIN RETURN 1; END'",
        )
        self._sql(
            "selected_main",
            "CREATE MATERIALIZED VIEW fixture_view AS SELECT fixture_action()",
        )
        self._sql(
            "selected_main",
            "CREATE OR REPLACE FUNCTION fixture_action() RETURNS int LANGUAGE plpgsql AS 'BEGIN CREATE ROLE fixture_escape_role; RETURN 1; END'",
        )
        manifest = self.adapter.backup(self.specs, self.work / "logical-postgres")
        with self.assertRaisesRegex(ServicePostgresError, "helper rejected"):
            self.adapter.prepare_restore(
                manifest,
                self.work / "logical-postgres",
                "service_test_job",
                self._persist,
                specs=self.specs,
            )
        self.assertEqual(
            self._sql(
                "postgres",
                "SELECT count(*) FROM pg_roles WHERE rolname='fixture_escape_role'",
                fetch=True,
            ),
            [(0,)],
        )
        self.adapter.rollback_restore(self.snapshots[-1], self._persist)
        self.assertEqual(
            self._sql("unrelated", "SELECT value FROM example", fetch=True),
            [("saved",)],
        )

    def test_foreign_object_owner_rejected_without_changing_owner_or_rows(self):
        role = "fixture_owner_" + secrets.token_hex(6)
        self._sql("postgres", sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
        self._sql(
            "selected_main",
            sql.SQL("ALTER TABLE example OWNER TO {}").format(sql.Identifier(role)),
        )
        self.assertEqual(
            self._sql(
                "selected_main",
                "SELECT relacl FROM pg_class WHERE oid='example'::regclass",
                fetch=True,
            ),
            [(None,)],
        )
        with self.assertRaisesRegex(ServicePostgresError, "custom object ownership"):
            self.adapter.estimate(self.specs)
        with self.assertRaisesRegex(ServicePostgresError, "custom object ownership"):
            self.adapter.backup(self.specs, self.work / "logical-postgres")
        self.assertEqual(
            self._sql(
                "selected_main",
                "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='example'::regclass",
                fetch=True,
            ),
            [(role,)],
        )
        self.assertEqual(
            self._sql("selected_main", "SELECT value FROM example", fetch=True),
            [("saved",)],
        )

    def test_default_type_and_large_object_acl_rejected_before_dump(self):
        role = "fixture_acl_" + secrets.token_hex(6)
        self._sql("postgres", sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
        self._sql(
            "selected_main",
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}"
            ).format(sql.Identifier(role)),
        )
        with self.assertRaisesRegex(ServicePostgresError, "custom object permissions"):
            self.adapter.estimate(self.specs)
        self._sql(
            "selected_main",
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM {}"
            ).format(sql.Identifier(role)),
        )
        self._sql("selected_main", "CREATE TYPE fixture_type AS ENUM ('one','two')")
        self._sql(
            "selected_main",
            sql.SQL("GRANT USAGE ON TYPE fixture_type TO {}").format(
                sql.Identifier(role)
            ),
        )
        with self.assertRaisesRegex(ServicePostgresError, "custom object permissions"):
            self.adapter.estimate(self.specs)
        self._sql("selected_main", "DROP TYPE fixture_type")
        oid = self._sql("selected_main", "SELECT lo_create(0)", fetch=True)[0][0]
        self._sql(
            "selected_main",
            sql.SQL("GRANT SELECT ON LARGE OBJECT {} TO {}").format(
                sql.Literal(oid), sql.Identifier(role)
            ),
        )
        with self.assertRaisesRegex(ServicePostgresError, "custom object permissions"):
            self.adapter.estimate(self.specs)
        self._sql("selected_main", "SELECT lo_unlink(%s)", (oid,))
        self.assertGreater(self.adapter.estimate(self.specs), 0)


if __name__ == "__main__":
    unittest.main()
