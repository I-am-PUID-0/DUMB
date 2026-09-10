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

    def test_get_config_value_reads_postgres_when_backend_is_postgres(self):
        # When InfiniDysk runs against PostgreSQL, the sqlite file this
        # module otherwise reads is stale/absent -- get_config_value must
        # read the live backend instead, not the on-disk sqlite fallback.
        self.nzbdav_db.CONFIG_MANAGER.get = lambda *args, **kwargs: {
            "postgres_enabled": True,
            "env": {
                "DATABASE_CONNECTION_STRING": (
                    'Host="127.0.0.1";Port="5432";Database="infinidysk_main";'
                    'Username="DUMB";Password="postgres"'
                ),
            },
        }

        fake_cursor = mock.MagicMock()
        fake_cursor.__enter__.return_value = fake_cursor
        fake_cursor.fetchone.return_value = ("live-postgres-value",)
        fake_connection = mock.MagicMock()
        fake_connection.cursor.return_value = fake_cursor

        with mock.patch("psycopg2.connect", return_value=fake_connection) as connect:
            value = self.nzbdav_db.get_config_value("api.key")

        self.assertEqual("live-postgres-value", value)
        _, kwargs = connect.call_args
        self.assertEqual("127.0.0.1", kwargs["host"])
        self.assertEqual("infinidysk_main", kwargs["dbname"])
        self.assertEqual("DUMB", kwargs["user"])

    def test_get_config_value_falls_back_to_sqlite_when_postgres_read_fails(self):
        self.nzbdav_db.CONFIG_MANAGER.get = lambda *args, **kwargs: {
            "postgres_enabled": True,
            "env": {
                "DATABASE_CONNECTION_STRING": (
                    'Host="127.0.0.1";Port="5432";Database="infinidysk_main";'
                    'Username="DUMB";Password="postgres"'
                ),
            },
        }

        with mock.patch(
            "psycopg2.connect", side_effect=OSError("connection refused")
        ):
            value = self.nzbdav_db.get_config_value("api.key")

        self.assertEqual("abc", value)

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
