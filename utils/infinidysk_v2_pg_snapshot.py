"""Read-only, verified PostgreSQL input snapshots for the native v1 importer.

The seven-table SQLite codec follows InfiniDysk beta.6's import/postgres.rs.
A full pg_dump from the SAME exported snapshot is independently restored and
verified before this module returns an acceptance receipt. This module has no
configuration-singleton imports and is safe for the isolated restore helper.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile

from utils.infinidysk_v2_subprocess import (
    MigrationProcessCleanupError,
    run_migration_process,
)

NATIVE_SOURCE_COMMIT = "6363f4a4a3f1bcbc3308142b66ec3e7d7127f853"
LEGACY_TABLES = (
    "__EFMigrationsHistory",
    "Accounts",
    "ConfigItems",
    "DavItems",
    "HistoryItems",
    "QueueItems",
    "QueueNzbContents",
)
MINIMUM_SCHEMA = "20260824160000"
MAX_TABLES = 256
MAX_COLUMNS = 256
MAX_ROW_BYTES = 256 * 1024 * 1024


class InfiniDyskV2PostgresSnapshotError(RuntimeError):
    pass


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _quote(value):
    return '"' + str(value).replace('"', '""') + '"'


def _connection_entries(text):
    if not isinstance(text, str) or not text or len(text) > 64 * 1024:
        raise InfiniDyskV2PostgresSnapshotError(
            "The active PostgreSQL connection string is unavailable or oversized."
        )
    result = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index] in " ;\t\r\n":
            index += 1
        if index == len(text):
            break
        begin = index
        while index < len(text) and text[index] not in "=;":
            index += 1
        if index == len(text) or text[index] != "=":
            raise InfiniDyskV2PostgresSnapshotError(
                "The active connection string is malformed."
            )
        name = text[begin:index].strip().lower()
        index += 1
        while index < len(text) and text[index].isspace():
            index += 1
        if index < len(text) and text[index] in "\"'":
            quote = text[index]
            index += 1
            value = []
            while index < len(text):
                character = text[index]
                index += 1
                if character == quote:
                    if index < len(text) and text[index] == quote:
                        value.append(quote)
                        index += 1
                        continue
                    break
                value.append(character)
            else:
                raise InfiniDyskV2PostgresSnapshotError(
                    "The active connection string has an unterminated quote."
                )
            while index < len(text) and text[index].isspace():
                index += 1
            if index < len(text) and text[index] != ";":
                raise InfiniDyskV2PostgresSnapshotError(
                    "The active connection string has trailing quoted content."
                )
            value = "".join(value)
        else:
            begin = index
            while index < len(text) and text[index] != ";":
                index += 1
            value = text[begin:index].strip()
        result.append((name, value))
        if index < len(text):
            index += 1
    return result


def connection_settings(plan):
    """Preserve supported source auth/TLS choices; never silently drop a setting."""
    service = plan["source"]["service"]
    env = service.get("env") or {}
    aliases = {
        "host": "host",
        "server": "host",
        "data source": "host",
        "port": "port",
        "database": "dbname",
        "initial catalog": "dbname",
        "username": "user",
        "user id": "user",
        "userid": "user",
        "user": "user",
        "password": "password",
        "pwd": "password",
        "ssl mode": "sslmode",
        "root certificate": "sslrootcert",
        "ssl certificate": "sslcert",
        "ssl key": "sslkey",
        "passfile": "passfile",
        "search path": "search_path",
    }
    driver_only = {
        "pooling",
        "minimum pool size",
        "maximum pool size",
        "timeout",
        "command timeout",
        "connection idle lifetime",
        "connection pruning interval",
        "keepalive",
        "read buffer size",
        "write buffer size",
        "max auto prepare",
        "auto prepare min usages",
        "no reset on close",
        "include error detail",
        "application name",
    }
    params = {}
    for name, value in _connection_entries(env.get("DATABASE_CONNECTION_STRING")):
        if name in aliases:
            params[aliases[name]] = value
        elif name not in driver_only:
            raise InfiniDyskV2PostgresSnapshotError(
                "The PostgreSQL source uses an unsupported connection option; no auth/TLS setting was weakened."
            )
    for name in ("host", "dbname", "user"):
        if not params.get(name):
            raise InfiniDyskV2PostgresSnapshotError(
                "The active PostgreSQL source must explicitly name its host, database and user."
            )
    if "," in params["host"]:
        raise InfiniDyskV2PostgresSnapshotError(
            "A multi-server PostgreSQL source requires a separately audited adapter."
        )
    search_path = params.pop("search_path", "public")
    if search_path.strip() != "public" or env.get("PGOPTIONS"):
        raise InfiniDyskV2PostgresSnapshotError(
            "This managed PostgreSQL snapshot requires an explicit public application schema."
        )
    try:
        params["port"] = int(params.get("port") or 5432)
    except ValueError:
        raise InfiniDyskV2PostgresSnapshotError(
            "The PostgreSQL source port is invalid."
        ) from None
    if not 1 <= params["port"] <= 65535:
        raise InfiniDyskV2PostgresSnapshotError(
            "The PostgreSQL source port is invalid."
        )
    modes = {
        "disable": "disable",
        "allow": "allow",
        "prefer": "prefer",
        "require": "require",
        "verifyca": "verify-ca",
        "verify-ca": "verify-ca",
        "verifyfull": "verify-full",
        "verify-full": "verify-full",
    }
    mode = (
        str(params.get("sslmode") or env.get("PGSSLMODE") or "prefer")
        .lower()
        .replace(" ", "")
    )
    if mode not in modes:
        raise InfiniDyskV2PostgresSnapshotError(
            "The source TLS policy cannot be represented safely."
        )
    params["sslmode"] = modes[mode]
    for field, variable in (
        ("sslrootcert", "PGSSLROOTCERT"),
        ("sslcert", "PGSSLCERT"),
        ("sslkey", "PGSSLKEY"),
        ("passfile", "PGPASSFILE"),
        ("password", "PGPASSWORD"),
    ):
        if field not in params and env.get(variable):
            params[field] = str(env[variable])
    params.update(
        connect_timeout=15,
        application_name="dumb-infinidysk-v2-snapshot",
        options="-c default_transaction_read_only=on -c search_path=public -c lock_timeout=30000",
    )
    return params


def _readonly(connection, timezone=None, *, long_running=False):
    connection.set_session(
        isolation_level="REPEATABLE READ", readonly=True, autocommit=False
    )
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '30s'")
        if long_running:
            # This transaction exports a snapshot for independently streamed
            # bulk work. Server defaults must not expire it merely for duration.
            cursor.execute("SET LOCAL statement_timeout = 0")
            cursor.execute("SET LOCAL idle_in_transaction_session_timeout = 0")
            cursor.execute("SELECT current_setting('transaction_timeout', true)")
            if cursor.fetchone()[0] is not None:
                # PostgreSQL 17+ can bound the entire transaction separately.
                # missing_ok keeps the bulk policy compatible with older servers.
                cursor.execute("SET LOCAL transaction_timeout = 0")
        cursor.execute("SET LOCAL DateStyle = 'ISO, YMD'")
        cursor.execute("SET LOCAL IntervalStyle = 'postgres'")
        cursor.execute("SET LOCAL bytea_output = 'hex'")
        cursor.execute("SET LOCAL extra_float_digits = 3")
        if timezone is not None:
            cursor.execute("SELECT set_config('TimeZone', %s, true)", (timezone,))
        cursor.execute("SELECT current_setting('transaction_read_only')")
        if cursor.fetchone()[0] != "on":
            raise InfiniDyskV2PostgresSnapshotError(
                "The source transaction is not read-only."
            )


def _rows_digest(rows, *, check_cancel=None, progress=None):
    cancel = check_cancel or (lambda: None)
    report = progress or (lambda count: None)
    cancel()
    count, xor, total = 0, 0, 0
    modulus = 1 << 256
    for row in rows:
        cancel()
        if any(value is not None and not isinstance(value, str) for value in row):
            raise InfiniDyskV2PostgresSnapshotError(
                "The PostgreSQL text snapshot codec returned an unexpected value."
            )
        raw = json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode()
        if len(raw) > MAX_ROW_BYTES:
            raise InfiniDyskV2PostgresSnapshotError(
                "A PostgreSQL source row exceeds the supported snapshot bound."
            )
        digest = int.from_bytes(hashlib.sha256(raw).digest(), "big")
        xor ^= digest
        total = (total + digest) % modulus
        count += 1
        if count % 1000 == 0:
            report(count)
    report(count)
    return {"count": count, "xor": f"{xor:064x}", "sum": f"{total:064x}"}


def _schema_objects(connection):
    """Compare restored constraints/indexes and sequence definitions AND state."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT n.nspname,c.relname,k.conname,k.contype,k.convalidated,"
            "pg_get_constraintdef(k.oid,true) FROM pg_constraint k "
            "JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg_%' "
            "ORDER BY n.nspname,c.relname,k.conname"
        )
        constraints = [list(row) for row in cursor.fetchall()]
        cursor.execute(
            "SELECT schemaname,tablename,indexname,indexdef FROM pg_indexes "
            "WHERE schemaname <> 'information_schema' AND schemaname NOT LIKE 'pg_%' "
            "ORDER BY schemaname,tablename,indexname"
        )
        indexes = [list(row) for row in cursor.fetchall()]
        cursor.execute(
            "SELECT n.nspname,c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid,true) "
            "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE NOT t.tgisinternal "
            "AND n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg_%' "
            "ORDER BY n.nspname,c.relname,t.tgname"
        )
        triggers = [list(row) for row in cursor.fetchall()]
        cursor.execute(
            "SELECT schemaname,sequencename,data_type::text,start_value,min_value,max_value,"
            "increment_by,cycle,cache_size FROM pg_sequences "
            "WHERE schemaname <> 'information_schema' AND schemaname NOT LIKE 'pg_%' "
            "ORDER BY schemaname,sequencename"
        )
        sequences = [list(row) for row in cursor.fetchall()]
        if max(len(constraints), len(indexes), len(triggers), len(sequences)) > 8192:
            raise InfiniDyskV2PostgresSnapshotError(
                "The PostgreSQL source schema-object inventory exceeds its bound."
            )
        sequence_state = {}
        for schema, name, *_ in sequences:
            cursor.execute(
                f"SELECT last_value::text,is_called FROM {_quote(schema)}.{_quote(name)}"
            )
            row = cursor.fetchone()
            sequence_state[f"{schema}.{name}"] = list(row)
    return {
        "constraints": constraints,
        "indexes": indexes,
        "triggers": triggers,
        "sequences": sequences,
        "sequence_state": sequence_state,
    }


def _database_inventory(connection, *, rows, check_cancel=None, progress=None):
    cancel = check_cancel or (lambda: None)
    report = progress or (lambda *args, **kwargs: None)
    cancel()
    with connection.cursor() as cursor:
        cursor.execute("SHOW TimeZone")
        timezone = cursor.fetchone()[0]
        cursor.execute(
            "SELECT pg_encoding_to_char(encoding),datcollate,datctype "
            "FROM pg_database WHERE datname=current_database()"
        )
        database_characteristics = list(cursor.fetchone())
        cursor.execute("SELECT pg_database_size(current_database())")
        database_size = int(cursor.fetchone()[0])
        if database_size <= 0:
            raise InfiniDyskV2PostgresSnapshotError(
                "The PostgreSQL source size is unavailable."
            )
        cursor.execute(
            "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE c.relkind='r' AND n.nspname <> 'information_schema' AND n.nspname NOT LIKE 'pg_%' "
            "ORDER BY n.nspname,c.relname"
        )
        tables = cursor.fetchall()
        if len(tables) > MAX_TABLES:
            raise InfiniDyskV2PostgresSnapshotError(
                "The PostgreSQL source table inventory exceeds its bound."
            )
        catalog = {}
        for schema, name in tables:
            cancel()
            cursor.execute(
                "SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,"
                "pg_get_expr(d.adbin,d.adrelid),a.attidentity,a.attgenerated,"
                "CASE WHEN a.attcollation=0 THEN NULL ELSE cn.nspname || '.' || co.collname END, "
                "co.collprovider,co.collisdeterministic,co.collversion "
                "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
                "LEFT JOIN pg_collation co ON co.oid=a.attcollation "
                "LEFT JOIN pg_namespace cn ON cn.oid=co.collnamespace "
                "WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum",
                (schema, name),
            )
            columns = cursor.fetchall()
            if not columns or len(columns) > MAX_COLUMNS:
                raise InfiniDyskV2PostgresSnapshotError(
                    "A PostgreSQL source table has an unsupported column inventory."
                )
            catalog[f"{schema}.{name}"] = {
                "schema": schema,
                "name": name,
                "columns": [list(column) for column in columns],
            }
        cursor.execute(
            'SELECT "MigrationId" FROM public."__EFMigrationsHistory" ORDER BY "MigrationId"'
        )
        history = [row[0] for row in cursor.fetchall()]
    terminal = str(history[-1]) if history else ""
    if terminal[:14] < MINIMUM_SCHEMA:
        raise InfiniDyskV2PostgresSnapshotError(
            "The PostgreSQL source is older than the native v1.3 schema floor."
        )
    objects = _schema_objects(connection)
    content = {}
    if rows:
        for index, (key, table) in enumerate(catalog.items(), start=1):
            cancel()
            columns = ",".join(
                f"{_quote(column[0])}::text" for column in table["columns"]
            )
            with connection.cursor(name=f"dumb_v2_{secrets.token_hex(8)}") as cursor:
                cursor.itersize = 16
                cursor.execute(
                    f"SELECT {columns} FROM {_quote(table['schema'])}.{_quote(table['name'])}"
                )
                content[key] = _rows_digest(
                    cursor,
                    check_cancel=cancel,
                    progress=lambda count: report(
                        "postgres_snapshot",
                        "Validating source database contents.",
                        table_index=index,
                        table_count=len(catalog),
                        rows=count,
                    ),
                )
    return {
        "session_timezone": timezone,
        "database_characteristics": database_characteristics,
        "database_size_bytes": database_size,
        "tables": catalog,
        "schema_objects": objects,
        "schema_fingerprint": _digest(
            {
                "tables": catalog,
                "database_characteristics": database_characteristics,
                "objects": {
                    key: value
                    for key, value in objects.items()
                    if key != "sequence_state"
                },
            }
        ),
        "migration_history_fingerprint": _digest(history),
        "terminal_migration": terminal,
        "table_digests": content,
    }


def _physical(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT system_identifier::text FROM pg_control_system()")
        system = str(cursor.fetchone()[0])
        cursor.execute(
            "SELECT oid::text FROM pg_database WHERE datname=current_database()"
        )
        database = str(cursor.fetchone()[0])
        cursor.execute("SELECT inet_server_addr()::text,inet_server_port()")
        address, port = cursor.fetchone()
    if not system.isdigit() or not database.isdigit():
        raise InfiniDyskV2PostgresSnapshotError(
            "The source cluster/database identity could not be proven."
        )
    return {"system_identifier": system, "database_oid": database}, {
        "address": address,
        "port": port,
    }


def _connect_default(**params):
    import psycopg2

    return psycopg2.connect(**params)


def inspect_postgres_source(plan, *, connect=None):
    connection = None
    try:
        connection = (connect or _connect_default)(**connection_settings(plan))
        _readonly(connection)
        physical, server = _physical(connection)
        binding = _database_inventory(connection, rows=False)
        binding.update(physical_identity=physical, server=server)
        return binding
    except InfiniDyskV2PostgresSnapshotError:
        raise
    except Exception:
        raise InfiniDyskV2PostgresSnapshotError(
            "The PostgreSQL source identity/schema could not be inspected safely."
        ) from None
    finally:
        if connection is not None:
            try:
                connection.rollback()
            finally:
                connection.close()


def _native_column(name, data_type):
    column = _quote(name)
    if data_type == "uuid":
        return f"upper({column}::text)", "TEXT", "text"
    if data_type == "boolean":
        return f"{column}::int::int8", "INTEGER", "int"
    if data_type in {"smallint", "integer", "bigint"}:
        return f"{column}::int8", "INTEGER", "int"
    return f"{column}::text", "TEXT", "text"


def _stage_legacy(connection, destination, binding, progress, *, check_cancel=None):
    cancel = check_cancel or (lambda: None)
    cancel()
    temporary = destination.with_suffix(".partial")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    os.close(descriptor)
    copied = {}
    try:
        with sqlite3.connect(temporary) as sqlite:
            sqlite.execute("PRAGMA journal_mode=OFF")
            sqlite.execute("PRAGMA synchronous=OFF")
            sqlite.execute("BEGIN")
            for name in LEGACY_TABLES:
                cancel()
                table = binding["tables"].get(f"public.{name}")
                if not table:
                    raise InfiniDyskV2PostgresSnapshotError(
                        "The source is missing a native import table."
                    )
                mapped = [
                    _native_column(column[0], column[1]) for column in table["columns"]
                ]
                definition = ",".join(
                    f"{_quote(column[0])} {mapping[1]}"
                    for column, mapping in zip(table["columns"], mapped)
                )
                sqlite.execute(f"CREATE TABLE {_quote(name)} ({definition})")
                select = ",".join(item[0] for item in mapped)
                with connection.cursor(
                    name=f"dumb_v2_stage_{secrets.token_hex(8)}"
                ) as cursor:
                    cursor.itersize = 16
                    cursor.execute(f"SELECT {select} FROM public.{_quote(name)}")
                    count = 0
                    for row in cursor:
                        cancel()
                        for value, mapping in zip(row, mapped):
                            if value is not None and (
                                (mapping[2] == "int" and type(value) is not int)
                                or (mapping[2] == "text" and not isinstance(value, str))
                            ):
                                raise InfiniDyskV2PostgresSnapshotError(
                                    "The native staging codec returned an unexpected type."
                                )
                        if (
                            sum(
                                len(value.encode())
                                for value in row
                                if isinstance(value, str)
                            )
                            > MAX_ROW_BYTES
                        ):
                            raise InfiniDyskV2PostgresSnapshotError(
                                "A native staging row exceeds its supported bound."
                            )
                        sqlite.execute(
                            f"INSERT INTO {_quote(name)} VALUES ({','.join('?' for _ in row)})",
                            row,
                        )
                        count += 1
                        if count % 1000 == 0:
                            progress(
                                "postgres_snapshot",
                                f"Copying native source table {name}.",
                                table=name,
                                rows=count,
                            )
                copied[name] = count
            sqlite.commit()
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        return copied
    finally:
        temporary.unlink(missing_ok=True)


def verify_restored_connection(connection, source_binding):
    """Verify a REAL restored database, including every backed-up user table."""
    try:
        _readonly(connection, source_binding["session_timezone"])
        restored_identity, _ = _physical(connection)
        if restored_identity == source_binding.get("physical_identity"):
            raise InfiniDyskV2PostgresSnapshotError(
                "The restore verifier must use an independent physical database."
            )
        restored = _database_inventory(connection, rows=True)
        for key in (
            "schema_fingerprint",
            "migration_history_fingerprint",
            "table_digests",
            "schema_objects",
        ):
            if restored[key] != source_binding[key]:
                raise InfiniDyskV2PostgresSnapshotError(
                    "The isolated PostgreSQL restore differs from the source snapshot."
                )
        return {
            "verified": True,
            "source_binding": source_binding["binding_fingerprint"],
            "tables_verified": len(restored["tables"]),
            "restored_identity": restored_identity,
        }
    finally:
        connection.rollback()


def _file_digest(path, *, check_cancel=None):
    cancel = check_cancel or (lambda: None)
    cancel()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            cancel()
            digest.update(chunk)
    return digest.hexdigest()


def _private_root(path):
    path = Path(path)
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise InfiniDyskV2PostgresSnapshotError(
                "PostgreSQL snapshot storage crosses a symbolic link."
            )
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = path.stat()
    if value.st_uid != os.geteuid() or not stat.S_ISDIR(value.st_mode):
        raise InfiniDyskV2PostgresSnapshotError(
            "PostgreSQL snapshots require controller-owned storage."
        )
    path.chmod(0o700)
    return path


def _space_check(requirements):
    """Conservative estimates, not guaranteed disk reservations."""
    grouped = {}
    for destination, needed in requirements:
        path = Path(destination)
        while not path.exists():
            path = path.parent
        item = grouped.setdefault(path.stat().st_dev, {"path": path, "needed": 0})
        item["needed"] += int(needed)
    for item in grouped.values():
        margin = max(64 * 1024 * 1024, item["needed"] // 10)
        if shutil.disk_usage(item["path"]).free < item["needed"] + margin:
            raise InfiniDyskV2PostgresSnapshotError(
                "Insufficient estimated free space for the PostgreSQL dump, typed staging and isolated restore; no backup write was started."
            )


def create_postgres_snapshot(
    plan,
    workspace,
    *,
    connect=None,
    run=None,
    verify_restore=None,
    progress=None,
    pg_dump=None,
    timeout=1800,
    check_cancel=None,
):
    if not callable(verify_restore):
        raise InfiniDyskV2PostgresSnapshotError(
            "A real isolated PostgreSQL restore verifier is required."
        )
    expected = plan["source"].get("postgres_binding")
    if not isinstance(expected, dict) or not expected.get("physical_identity"):
        raise InfiniDyskV2PostgresSnapshotError(
            "Run a fresh PostgreSQL source identity preflight first."
        )
    params = connection_settings(plan)
    root = _private_root(workspace["root"])
    dump = root / "v1-postgres.dump"
    staged = root / "v1-postgres-import.sqlite"
    if any(path.exists() or path.is_symlink() for path in (dump, staged)):
        raise InfiniDyskV2PostgresSnapshotError(
            "A snapshot artifact already occupies this private job."
        )
    program = pg_dump or shutil.which("pg_dump")
    if not program:
        raise InfiniDyskV2PostgresSnapshotError(
            "A compatible pg_dump binary is unavailable."
        )
    runner = run or subprocess.run
    cancel_callback = check_cancel or (lambda: None)
    progress_callback = progress or (lambda *args, **kwargs: None)
    callback_error = None

    def check():
        nonlocal callback_error
        try:
            cancel_callback()
        except Exception as error:
            callback_error = error
            raise

    def report(*args, **kwargs):
        nonlocal callback_error
        check()
        try:
            progress_callback(*args, **kwargs)
        except Exception as error:
            # Cancellation can also be raised by the manager's retained
            # progress callback. Preserve that trusted callback's exact error.
            callback_error = error
            raise

    def dump_progress(status):
        warning = bool(status.get("stall_warning"))
        report(
            "postgres_dump",
            (
                "No recent PostgreSQL dump activity was observed. The backup is "
                "still running; slow storage can take longer. You can cancel the migration."
                if warning
                else "Writing a full PostgreSQL backup from the same read-only snapshot."
            ),
            elapsed_seconds=int(status["elapsed_seconds"]),
            idle_seconds=int(status["idle_seconds"]),
            stall_warning=warning,
        )

    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    for key, variable in (
        ("host", "PGHOST"),
        ("port", "PGPORT"),
        ("dbname", "PGDATABASE"),
        ("user", "PGUSER"),
        ("password", "PGPASSWORD"),
        ("sslmode", "PGSSLMODE"),
        ("sslrootcert", "PGSSLROOTCERT"),
        ("sslcert", "PGSSLCERT"),
        ("sslkey", "PGSSLKEY"),
        ("passfile", "PGPASSFILE"),
        ("options", "PGOPTIONS"),
    ):
        if key in params:
            environment[variable] = str(params[key])
    connection = None
    temporary = dump.with_suffix(".partial")
    retain_partial = False
    try:
        check()
        connection = (connect or _connect_default)(**params)
        _readonly(connection, long_running=True)
        physical, server = _physical(connection)
        binding = _database_inventory(
            connection, rows=True, check_cancel=check, progress=report
        )
        binding.update(physical_identity=physical, server=server)
        for key in (
            "physical_identity",
            "schema_fingerprint",
            "migration_history_fingerprint",
        ):
            if binding[key] != expected[key]:
                raise InfiniDyskV2PostgresSnapshotError(
                    "The PostgreSQL source identity/schema changed after preview."
                )
        database_size = binding["database_size_bytes"]
        _space_check(
            [
                (root, database_size * 6),
                (tempfile.gettempdir(), database_size * 3),
            ]
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('server_version_num')::int")
            server_major = int(cursor.fetchone()[0]) // 10000
            cursor.execute("SELECT pg_export_snapshot()")
            exported = cursor.fetchone()[0]
        if not re.fullmatch(r"[0-9A-Fa-f-]+", exported):
            raise InfiniDyskV2PostgresSnapshotError(
                "The exported PostgreSQL snapshot identity is invalid."
            )
        version = runner(
            [str(program), "--version"],
            capture_output=True,
            check=True,
            timeout=15,
            env=environment,
        )
        output = (
            version.stdout.decode()
            if isinstance(version.stdout, bytes)
            else str(version.stdout)
        )
        match = re.search(r"pg_dump.*?(\d+)(?:\.\d+)", output)
        if not match or int(match.group(1)) != server_major:
            raise InfiniDyskV2PostgresSnapshotError(
                "pg_dump must match the PostgreSQL source major version."
            )
        staged_counts = _stage_legacy(
            connection, staged, binding, report, check_cancel=check
        )
        _space_check([(root, database_size * 2)])
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        os.close(descriptor)
        if server["address"]:
            environment["PGHOSTADDR"] = server["address"]
            environment["PGPORT"] = str(server["port"])
        report(
            "postgres_dump",
            "Writing a full PostgreSQL backup from the same read-only snapshot.",
        )
        command = [
            str(program),
            "--format=custom",
            "--no-password",
            "--snapshot",
            exported,
            "--file",
            str(temporary),
        ]
        child_environment = dict(environment)
        child_environment["PGOPTIONS"] = (
            str(child_environment.get("PGOPTIONS") or "")
            + " -c statement_timeout=0 -c idle_in_transaction_session_timeout=0"
        ).strip()
        if server_major >= 17:
            child_environment["PGOPTIONS"] += " -c transaction_timeout=0"
        check()
        if run is None:
            dumped = run_migration_process(
                command,
                env=child_environment,
                check_cancel=check,
                progress=dump_progress,
                activity_paths=(temporary,),
                idle_warning_seconds=timeout,
            )
        else:
            # Explicit test/adapter injection remains synchronous. Production
            # always uses the owned, cancellable process monitor above.
            dumped = runner(
                command,
                capture_output=True,
                check=True,
                timeout=None,
                env=child_environment,
            )
        check()
        if dumped.returncode:
            raise InfiniDyskV2PostgresSnapshotError(
                "The PostgreSQL backup process failed; original data was unchanged."
            )
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise InfiniDyskV2PostgresSnapshotError("The PostgreSQL backup is empty.")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, dump)
        binding["binding_fingerprint"] = _digest(binding)
        connection.rollback()
        connection.close()
        connection = None
        report(
            "postgres_restore_verification",
            "Restoring and validating the independent PostgreSQL backup.",
        )
        _space_check([(tempfile.gettempdir(), dump.stat().st_size + database_size * 2)])
        check()
        proof = verify_restore(
            str(dump), copy_binding := json.loads(json.dumps(binding)), dict(params)
        )
        if (
            not isinstance(proof, dict)
            or proof.get("verified") is not True
            or proof.get("source_binding") != copy_binding["binding_fingerprint"]
        ):
            raise InfiniDyskV2PostgresSnapshotError(
                "The PostgreSQL backup has no matching successful isolated restore proof."
            )
        check()
        receipt = {
            "dump_path": str(dump),
            "staged_sqlite": str(staged),
            "verified_restore": True,
            "source_read_only": True,
            "physical_identity": physical,
            "source_binding": binding,
            "dump_sha256": _file_digest(dump, check_cancel=check),
            "staged_sha256": _file_digest(staged, check_cancel=check),
            "staged_table_counts": staged_counts,
            "native_source_commit": NATIVE_SOURCE_COMMIT,
            "restore_proof": proof,
        }
        metadata = root / "v1-postgres-snapshot.json"
        descriptor = os.open(
            metadata, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as handle:
            json.dump(receipt, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return receipt
    except MigrationProcessCleanupError:
        # The owned writer could not be proven reaped. Keep its private
        # artifact and let the coordinator retain the mutation admission.
        retain_partial = True
        raise
    except InfiniDyskV2PostgresSnapshotError:
        raise
    except Exception as error:
        if error is callback_error:
            raise
        # Driver/tool exceptions can contain credentials or row values.
        raise InfiniDyskV2PostgresSnapshotError(
            "The PostgreSQL source snapshot or isolated restore verification failed."
        ) from None
    finally:
        if not retain_partial:
            temporary.unlink(missing_ok=True)
        if connection is not None:
            # A lost server/connection must not replace a cancellation or an
            # unreaped-worker signal with a driver error containing credentials.
            active_error = sys.exc_info()[0] is not None
            cleanup_failed = False
            for cleanup in (connection.rollback, connection.close):
                try:
                    cleanup()
                except Exception:
                    cleanup_failed = True
            if cleanup_failed and not active_error:
                raise InfiniDyskV2PostgresSnapshotError(
                    "The read-only PostgreSQL snapshot connection did not close cleanly."
                ) from None
