import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock


class NzbDavDbSecurityTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "db.sqlite")

        fake_logger_module = types.ModuleType("utils.global_logger")
        fake_logger_module.logger = mock.Mock()

        fake_loader = types.ModuleType("utils.config_loader")
        fake_loader.CONFIG_MANAGER = types.SimpleNamespace(
            get=lambda *args, **kwargs: {},
        )

        self._module_stub = {
            "utils.global_logger": fake_logger_module,
            "utils.config_loader": fake_loader,
        }

        with mock.patch.dict(sys.modules, self._module_stub):
            if "utils.nzbdav_db" in sys.modules:
                sys.modules.pop("utils.nzbdav_db")
            import utils.nzbdav_db

            self.nzbdav_db = utils.nzbdav_db

        self._module_patcher = mock.patch.object(
            self.nzbdav_db,
            "_get_config_path",
            return_value=self._tmpdir.name,
        )
        self._module_patcher.start()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE ConfigItems (ConfigName TEXT PRIMARY KEY, ConfigValue TEXT)"
            )
            conn.execute(
                "INSERT INTO ConfigItems (ConfigName, ConfigValue) VALUES (?, ?)",
                ("api.key", "abc"),
            )
            conn.commit()

    def tearDown(self):
        self._module_patcher.stop()
        self._tmpdir.cleanup()
        for name in ("utils.nzbdav_db", "utils.global_logger", "utils.config_loader"):
            sys.modules.pop(name, None)

    def test_active_sqlite_read_preserves_config_path_and_ordinary_lookup(self):
        with mock.patch("psycopg2.connect") as connect:
            self.assertEqual(self.nzbdav_db.get_config_value("api.key"), "abc")
            self.assertEqual(
                self.nzbdav_db.get_active_config_value(
                    "api.key", {"env": {"CONFIG_PATH": self._tmpdir.name}}
                ),
                "abc",
            )
        connect.assert_not_called()

    def test_active_sqlite_reader_is_read_only_bounded_and_closes_connection(self):
        original_connect = sqlite3.connect
        connections = []

        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute(
                    "UPDATE ConfigItems SET ConfigValue = 'must-not-write'"
                )
            tracked = mock.Mock(wraps=connection)
            connections.append(tracked)
            return tracked

        with mock.patch.object(
            self.nzbdav_db.sqlite3, "connect", side_effect=connect
        ) as open_database:
            value = self.nzbdav_db.get_active_config_value(
                "api.key",
                {
                    "config_dir": "/unused-config-path",
                    "env": {"CONFIG_PATH": self._tmpdir.name},
                },
            )
        self.assertEqual(value, "abc")
        self.assertTrue(open_database.call_args.args[0].endswith("/db.sqlite?mode=ro"))
        self.assertEqual(open_database.call_args.kwargs, {"uri": True, "timeout": 2})
        connections[0].execute.assert_called_once_with(
            "SELECT ConfigValue FROM ConfigItems WHERE ConfigName = ?", ("api.key",)
        )
        connections[0].close.assert_called_once()
        with original_connect(self.db_path) as connection:
            self.assertEqual(
                connection.execute("SELECT ConfigValue FROM ConfigItems").fetchone()[0],
                "abc",
            )

    def test_active_sqlite_missing_or_disappearing_database_is_not_created(self):
        original_connect = sqlite3.connect
        for disappear in (False, True):
            with (
                self.subTest(disappear=disappear),
                tempfile.TemporaryDirectory() as tmp,
            ):
                database = os.path.join(tmp, "db.sqlite")
                if disappear:
                    with original_connect(database) as connection:
                        connection.execute(
                            "CREATE TABLE ConfigItems (ConfigName TEXT, ConfigValue TEXT)"
                        )

                def connect(*args, **kwargs):
                    if os.path.exists(database):
                        os.unlink(database)
                    return original_connect(*args, **kwargs)

                with mock.patch.object(
                    self.nzbdav_db.sqlite3, "connect", side_effect=connect
                ):
                    with self.assertRaises(sqlite3.OperationalError):
                        self.nzbdav_db.get_active_config_value(
                            "api.key", {"config_dir": tmp}
                        )
                self.assertFalse(os.path.exists(database))

    def test_active_postgres_read_is_read_only_qualified_and_closes_connection(self):
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("postgres-fixture-key",)
        config = {
            "env": {
                "DATABASE_PROVIDER": "PostgreSQL",
                "DATABASE_CONNECTION_STRING": "Host=127.0.0.2;Port=5544;Database=fixture_db;"
                'Username=fixture_user;Password="fixture;password"',
            }
        }
        with (
            mock.patch("psycopg2.connect", return_value=connection) as connect,
            mock.patch.object(
                self.nzbdav_db,
                "_connect",
                side_effect=AssertionError("retained SQLite must not be read"),
            ),
        ):
            value = self.nzbdav_db.get_active_config_value("api.key", config)
        self.assertEqual(value, "postgres-fixture-key")
        self.assertEqual(connect.call_args.kwargs["host"], "127.0.0.2")
        self.assertEqual(connect.call_args.kwargs["port"], "5544")
        self.assertEqual(connect.call_args.kwargs["dbname"], "fixture_db")
        self.assertEqual(connect.call_args.kwargs["password"], "fixture;password")
        self.assertEqual(connect.call_args.kwargs["connect_timeout"], 2)
        connection.set_session.assert_called_once_with(readonly=True, autocommit=True)
        self.assertEqual(
            cursor.execute.call_args_list[0].args, ("SET statement_timeout = '2s'",)
        )
        self.assertEqual(
            cursor.execute.call_args_list[1].args,
            (
                'SELECT "ConfigValue" FROM "public"."ConfigItems" WHERE "ConfigName" = %s',
                ("api.key",),
            ),
        )
        connection.close.assert_called_once()

    def test_postgres_missing_row_or_failure_never_reads_retained_sqlite(self):
        for outcome in (None, OSError("fixture PostgreSQL unavailable")):
            with self.subTest(outcome=type(outcome).__name__):
                connection = mock.MagicMock()
                cursor = connection.cursor.return_value.__enter__.return_value
                cursor.fetchone.return_value = None
                with (
                    mock.patch(
                        "psycopg2.connect", side_effect=outcome, return_value=connection
                    ),
                    mock.patch.object(
                        self.nzbdav_db,
                        "_connect",
                        side_effect=AssertionError("retained SQLite must not be read"),
                    ),
                ):
                    if outcome is None:
                        self.assertIsNone(
                            self.nzbdav_db.get_active_config_value(
                                "api.key", {"postgres_enabled": True}
                            )
                        )
                        connection.close.assert_called_once()
                    else:
                        with self.assertRaises(OSError):
                            self.nzbdav_db.get_active_config_value(
                                "api.key", {"postgres_enabled": True}
                            )

    def test_postgres_query_failure_also_closes_connection(self):
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.execute.side_effect = OSError("fixture query unavailable")
        with mock.patch("psycopg2.connect", return_value=connection):
            with self.assertRaises(OSError):
                self.nzbdav_db.get_active_config_value(
                    "api.key", {"postgres_enabled": True}
                )
        connection.close.assert_called_once()

    def test_fetch_rows_rejects_unsafe_table_name(self):
        with self.assertRaises(ValueError):
            self.nzbdav_db.fetch_rows("ConfigItems;DROP TABLE ConfigItems")

    def test_fetch_rows_rejects_unknown_table(self):
        with self.assertRaises(ValueError):
            self.nzbdav_db.fetch_rows("not_a_table")

    def test_upsert_row_rejects_unsafe_table_name(self):
        with self.assertRaises(ValueError):
            self.nzbdav_db.upsert_row(
                "ConfigItems;DROP", {"ConfigName": "x", "ConfigValue": "y"}
            )

    def test_upsert_row_rejects_unsafe_data_column(self):
        with self.assertRaises(ValueError):
            self.nzbdav_db.upsert_row(
                "ConfigItems",
                {"ConfigName;DROP": "x", "ConfigValue": "y"},
                key_columns=["ConfigName"],
            )

    def test_upsert_row_rejects_unknown_data_column(self):
        with self.assertRaises(ValueError):
            self.nzbdav_db.upsert_row(
                "ConfigItems",
                {"ConfigName": "x", "NotARealColumn": "y"},
                key_columns=["ConfigName"],
            )

    def test_upsert_row_rejects_invalid_key_columns_type(self):
        with self.assertRaises(ValueError):
            self.nzbdav_db.upsert_row(
                "ConfigItems",
                {"ConfigName": "x", "ConfigValue": "y"},
                key_columns="ConfigName",
            )

    def test_upsert_row_rejects_non_iterable_key_columns(self):
        with self.assertRaises(ValueError):
            self.nzbdav_db.upsert_row(
                "ConfigItems",
                {"ConfigName": "x", "ConfigValue": "y"},
                key_columns=123,
            )

    def test_upsert_row_rejects_unknown_key_column(self):
        with self.assertRaises(ValueError):
            self.nzbdav_db.upsert_row(
                "ConfigItems",
                {"ConfigName": "x", "ConfigValue": "y"},
                key_columns=["MissingKey"],
            )

    def test_get_table_columns_rejects_unsafe_table_name(self):
        with self.assertRaises(ValueError):
            self.nzbdav_db.get_table_columns("ConfigItems --")

    def test_delete_rows_rejects_oversized_where_clause(self):
        ok, err = self.nzbdav_db.delete_rows("ConfigItems", "x" * 600)
        self.assertFalse(ok)
        self.assertIn("too long", err)


if __name__ == "__main__":
    unittest.main()
