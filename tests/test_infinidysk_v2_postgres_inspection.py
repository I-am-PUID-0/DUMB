"""Destination preflight preserves physical identity and actionable private errors."""

import json
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import psycopg2

from utils.infinidysk_v2_postgres import NativePostgresError, inspect_target


class DriverFailure(psycopg2.DatabaseError):
    def __init__(self, code):
        super().__init__(
            "postgres://fixture:fixture-only-secret@db.example.invalid/private"
        )
        self.code = code

    @property
    def pgcode(self):
        return self.code


class TargetInspectionTests(unittest.TestCase):
    def setUp(self):
        self.target = {
            "host": "db.example.invalid",
            "port": 5432,
            "dbname": "dedicated_target",
            "user": "fixture_role",
            "password": "fixture-only-secret",
            "sslmode": "verify-full",
        }

    def connection(self, *, error=None, schema="public", create=True, relations=()):
        class Cursor:
            def __init__(self):
                self.queries = []
                self.current = ""

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, query):
                self.current = query
                self.queries.append(query)
                if error and "pg_control_system()" in query:
                    raise error

            def fetchone(self):
                if "FROM pg_catalog.pg_control_system()" in self.current:
                    return ("11",)
                if "FROM pg_control_system()" in self.current:
                    # A public function can shadow an unqualified builtin when
                    # search_path explicitly puts public before pg_catalog.
                    return ("42",)
                if "FROM pg_catalog.pg_database" in self.current:
                    return ("22", "C")
                if "pg_database" in self.current:
                    return ("99", "C")
                if "current_schema" in self.current:
                    return (schema, "fixture_role")
                if "has_schema_privilege" in self.current:
                    return (create,)
                raise AssertionError("Unexpected fixture query")

            def fetchall(self):
                return list(relations)

        cursor = Cursor()
        return (
            SimpleNamespace(set_session=Mock(), cursor=lambda: cursor, close=Mock()),
            cursor,
        )

    def assert_private_failure(self, callback, expected):
        with self.assertRaisesRegex(NativePostgresError, expected) as caught:
            callback()
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("fixture-only-secret", rendered)
        self.assertNotIn("postgres://", rendered)
        self.assertNotIn("db.example.invalid", rendered)
        return caught.exception

    def test_catalog_qualified_identity_ignores_search_path_shadowing(self):
        connection, cursor = self.connection()
        with patch(
            "utils.infinidysk_v2_postgres.psycopg2.connect",
            return_value=connection,
        ):
            result = inspect_target(self.target, require_empty=True)
        self.assertEqual(
            {
                "system_identifier": "11",
                "database_oid": "22",
                "schema": "public",
                "owner": "fixture_role",
                "collation": "C",
                "relations": [],
            },
            result,
        )
        connection.set_session.assert_called_once_with(readonly=True, autocommit=True)
        connection.close.assert_called_once_with()
        self.assertTrue(all(query.startswith("SELECT ") for query in cursor.queries))
        self.assertTrue(any("pg_catalog.pg_class" in query for query in cursor.queries))
        self.assertTrue(
            any("pg_catalog.pg_namespace" in query for query in cursor.queries)
        )

    def test_identity_permission_failure_names_the_narrow_grant(self):
        connection, _ = self.connection(error=DriverFailure("42501"))
        with patch(
            "utils.infinidysk_v2_postgres.psycopg2.connect",
            return_value=connection,
        ):
            error = self.assert_private_failure(
                lambda: inspect_target(self.target, require_empty=True),
                "EXECUTE on pg_catalog.pg_control_system",
            )
        self.assertNotIn("superuser", str(error))
        self.assertIn("will not skip", str(error))
        connection.close.assert_called_once_with()

    def test_unavailable_physical_identity_does_not_fall_back_to_host_or_name(self):
        connection, _ = self.connection(error=DriverFailure("42883"))
        with patch(
            "utils.infinidysk_v2_postgres.psycopg2.connect",
            return_value=connection,
        ):
            self.assert_private_failure(
                lambda: inspect_target(self.target), "physical cluster identity"
            )
        connection.close.assert_called_once_with()

    def test_missing_database_connection_without_sqlstate_is_actionable(self):
        error = psycopg2.OperationalError(
            'connection failed: FATAL:  database "dedicated_target" does not exist\n'
            "password=fixture-only-secret"
        )
        self.assertIsNone(error.pgcode)
        with patch("utils.infinidysk_v2_postgres.psycopg2.connect", side_effect=error):
            result = self.assert_private_failure(
                lambda: inspect_target(self.target), "database does not exist"
            )
        self.assertIn("Create a new empty database with C collation", str(result))
        self.assertIn("only reuses its connection settings", str(result))

    def test_missing_database_sqlstate_and_authentication_diagnostics_are_private(self):
        for error, expected in (
            (DriverFailure("3D000"), "database does not exist"),
            (DriverFailure("28P01"), "rejected the destination credentials"),
            (DriverFailure("28000"), "rejected the destination credentials"),
            (
                psycopg2.OperationalError('FATAL: role "fixture_role" does not exist'),
                "rejected the destination credentials",
            ),
            (
                psycopg2.OperationalError(
                    "FATAL: password authentication failed for user fixture-only-secret"
                ),
                "rejected the destination credentials",
            ),
            (
                psycopg2.OperationalError(
                    "FATAL: no pg_hba.conf entry for host fixture-only-secret"
                ),
                "rejected the destination credentials",
            ),
        ):
            with (
                self.subTest(error_type=type(error).__name__),
                patch(
                    "utils.infinidysk_v2_postgres.psycopg2.connect", side_effect=error
                ),
            ):
                self.assert_private_failure(
                    lambda: inspect_target(self.target), expected
                )

    def test_unknown_or_localized_connection_failure_stays_private_and_blocked(self):
        for detail in (
            "postgres://fixture:fixture-only-secret@db.example.invalid/private",
            'database "another_database" does not exist fixture-only-secret',
        ):
            with patch(
                "utils.infinidysk_v2_postgres.psycopg2.connect",
                side_effect=psycopg2.OperationalError(detail),
            ):
                error = self.assert_private_failure(
                    lambda: inspect_target(self.target), "could not be verified"
                )
            self.assertIn("server is running and reachable", str(error))
            self.assertIn("TLS", str(error))

    def test_catalog_permission_failure_does_not_suggest_an_identity_bypass(self):
        with patch(
            "utils.infinidysk_v2_postgres.psycopg2.connect",
            side_effect=DriverFailure("42501"),
        ):
            error = self.assert_private_failure(
                lambda: inspect_target(self.target), "denied inspection"
            )
        self.assertNotIn("grant this role EXECUTE", str(error))

    def test_readable_but_unwritable_schema_is_rejected_without_mutation(self):
        connection, cursor = self.connection(create=False)
        with patch(
            "utils.infinidysk_v2_postgres.psycopg2.connect",
            return_value=connection,
        ):
            self.assert_private_failure(
                lambda: inspect_target(self.target, require_empty=True),
                "cannot create tables",
            )
        self.assertTrue(all(query.startswith("SELECT ") for query in cursor.queries))
        connection.close.assert_called_once_with()

    def test_nonempty_and_nonpublic_targets_remain_blocked(self):
        for kwargs, expected in (
            ({"relations": [("public", "existing_app")]}, "new empty database"),
            ({"schema": "custom"}, "dedicated public schema"),
        ):
            connection, _ = self.connection(**kwargs)
            with patch(
                "utils.infinidysk_v2_postgres.psycopg2.connect",
                return_value=connection,
            ):
                self.assert_private_failure(
                    lambda: inspect_target(self.target, require_empty=True), expected
                )
            connection.close.assert_called_once_with()

    def test_existing_relations_are_still_observable_for_bound_job_rechecks(self):
        connection, _ = self.connection(relations=[("public", "files")])
        with patch(
            "utils.infinidysk_v2_postgres.psycopg2.connect",
            return_value=connection,
        ):
            result = inspect_target(self.target)
        self.assertEqual([["public", "files"]], result["relations"])
        self.assertNotIn("fixture-only-secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
