"""Explicit, non-destructive preparation of a native managed PG destination."""

from pathlib import Path
import os

import psutil
import psycopg2
from psycopg2 import sql

from utils.infinidysk_v2_postgres import (
    NativePostgresError,
    _request,
    _require_collation,
    _target,
)

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_LOCK = 5333693572417501168


def _reserved_names(config):
    names = {"postgres", "template0", "template1"}
    postgres = config.get("postgres") or {}
    for item in postgres.get("databases") or []:
        if isinstance(item, dict) and item.get("name"):
            names.add(str(item["name"]))

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if (
                    isinstance(child, str)
                    and key.startswith("postgres_")
                    and (key.endswith("database") or key.endswith("db"))
                ):
                    names.add(child)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(getattr(config, "config", {}))
    return names


def _managed_process(manager, postgres):
    handler = manager.process_handler
    name = str(postgres.get("process_name") or "PostgreSQL")
    if handler is None or postgres.get("enabled") is not True:
        raise NativePostgresError(
            "Enable and start DUMB's PostgreSQL service before preparing its database."
        )
    internal = handler._prefixed_name(name)
    process = handler.process_names.get(internal)
    if process is None or process.poll() is not None:
        raise NativePostgresError(
            "Start DUMB's PostgreSQL service before preparing its database."
        )
    return process


def _prove_cluster(cursor, postgres, process, target):
    cursor.execute(
        "SELECT system_identifier::text, "
        "pg_catalog.current_setting('server_version_num')::int, "
        "pg_catalog.current_setting('data_directory'), "
        "pg_catalog.pg_backend_pid(), pg_catalog.host(pg_catalog.inet_server_addr()), "
        "pg_catalog.inet_server_port() FROM pg_catalog.pg_control_system()"
    )
    row = cursor.fetchone()
    directory = Path(str(postgres.get("config_dir") or "/postgres_data"))
    if (
        not row
        or not str(row[0]).isdigit()
        or int(row[1]) < 150000
        or not directory.is_absolute()
        or os.path.realpath(str(row[2])) != os.path.realpath(directory)
        or row[4] not in {"127.0.0.1", "::1"}
        or row[5] != target["port"]
    ):
        raise NativePostgresError(
            "The endpoint is not DUMB's configured local PostgreSQL 15-or-newer cluster."
        )
    try:
        lines = (directory / "postmaster.pid").read_text().splitlines()
        postmaster = int(lines[0])
        tracked = psutil.Process(process.pid)
        owned = {
            tracked.pid,
            *(child.pid for child in tracked.children(recursive=True)),
        }
        if (
            postmaster not in owned
            or psutil.Process(int(row[3])).ppid() != postmaster
            or os.path.realpath(lines[1]) != os.path.realpath(directory)
            or int(lines[3]) != target["port"]
            or process.poll() is not None
        ):
            raise ValueError("cluster ownership mismatch")
    except (OSError, ValueError, IndexError, psutil.Error):
        raise NativePostgresError(
            "The PostgreSQL server cannot be verified as DUMB's running managed process."
        ) from None
    return str(row[0])


def _database(cursor, name, user):
    cursor.execute(
        "SELECT d.oid::text, r.rolname, pg_catalog.pg_encoding_to_char(d.encoding), "
        "d.datcollate, d.datctype, d.datlocprovider, d.datallowconn, d.datistemplate "
        "FROM pg_catalog.pg_database d JOIN pg_catalog.pg_roles r ON r.oid=d.datdba "
        "WHERE d.datname=%s",
        (name,),
    )
    row = cursor.fetchone()
    if row and (
        row[1] != user
        or tuple(row[2:6]) != ("UTF8", "C", "C", "c")
        or row[6] is not True
        or row[7] is not False
    ):
        raise NativePostgresError(
            "The existing database has a different owner, locale or connection policy. "
            "Choose a new dedicated name; DUMB will not alter it."
        )
    return str(row[0]) if row else None


def _require_pristine(target, connect):
    connection = connect(
        **target,
        connect_timeout=5,
        application_name="dumb-infinidysk-v2-prepare",
        options="-c statement_timeout=10000 -c lock_timeout=5000",
    )
    try:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname NOT IN "
                "('public','information_schema') AND nspname !~ '^pg_' "
                "UNION ALL SELECT 1 FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND n.nspname !~ '^pg_' "
                "UNION ALL SELECT 1 FROM pg_catalog.pg_proc p "
                "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND n.nspname !~ '^pg_' "
                "UNION ALL SELECT 1 FROM pg_catalog.pg_type t "
                "JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace "
                "WHERE n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND n.nspname !~ '^pg_' "
                "UNION ALL SELECT 1 FROM pg_catalog.pg_extension "
                "WHERE extname <> 'plpgsql' "
                "UNION ALL SELECT 1 FROM pg_catalog.pg_largeobject_metadata "
                "UNION ALL SELECT 1 FROM pg_catalog.pg_event_trigger "
                "UNION ALL SELECT 1 FROM pg_catalog.pg_foreign_server)"
            )
            if cursor.fetchone()[0]:
                raise NativePostgresError(
                    "The destination already contains database objects. "
                    "Choose a new dedicated name; DUMB will not empty it."
                )
    finally:
        connection.close()


def prepare_managed_target(manager, options, *, connect=None):
    """Run under the native PostgreSQL API's shared mutation admission lane.

    This creates only one empty database. It never starts services, changes
    roles, overwrites credentials, changes the current provider, or cleans up
    an existing target. A subsequent preview still performs native validation.
    """
    options = _request(options)
    values = _request(options["target"]) if "target" in options else options
    if not (
        options.get("use_managed_postgres") is True
        or values.get("use_managed_postgres") is True
    ):
        raise NativePostgresError(
            "DUMB can prepare only its own managed PostgreSQL database."
        )
    config = manager._config()
    postgres = config.get("postgres") or {}
    target = _target(options, postgres)
    for key, expected in (
        ("host", target["host"]),
        ("port", target["port"]),
        ("user", target["user"]),
    ):
        if key in values and str(values[key]) != str(expected):
            raise NativePostgresError(
                "Managed database preparation cannot override PostgreSQL connection settings."
            )
    if target["host"] not in _LOOPBACK:
        raise NativePostgresError(
            "DUMB cannot create databases on an external PostgreSQL server."
        )
    if target["dbname"] in _reserved_names(config):
        raise NativePostgresError(
            "Choose a dedicated database name that is not reserved for another service."
        )
    process = _managed_process(manager, postgres)
    from utils.infinidysk_v2_update import native_update_state

    config_path = getattr(config, "file_path", None)
    if not isinstance(config_path, (str, os.PathLike)):
        config_path = "/config/dumb_config.json"
    state = native_update_state(
        manager._service(), Path(config_path).parent / "updates" / "infinidysk-v2"
    )
    if state and state.get("recovery_required"):
        raise NativePostgresError(
            "Finish the native runtime update recovery before preparing a database."
        )
    connect = connect or psycopg2.connect
    maintenance = {**target, "dbname": "postgres"}
    # Pin localhost to a numeric loopback rather than trusting name resolution.
    maintenance["hostaddr"] = "::1" if target["host"] == "::1" else "127.0.0.1"
    target = {**target, "hostaddr": maintenance["hostaddr"]}
    created = False
    connection = None
    try:
        connection = connect(
            **maintenance,
            connect_timeout=5,
            application_name="dumb-infinidysk-v2-prepare",
            options="-c statement_timeout=10000 -c lock_timeout=5000",
        )
        connection.autocommit = True
        with connection.cursor() as cursor:
            system = _prove_cluster(cursor, postgres, process, target)
            cursor.execute("SELECT pg_catalog.pg_try_advisory_lock(%s)", (_LOCK,))
            if cursor.fetchone()[0] is not True:
                raise NativePostgresError(
                    "Another database preparation is running. Try again when it finishes."
                )
            cursor.execute(
                "SELECT current_user, rolcanlogin, rolsuper OR rolcreatedb "
                "FROM pg_catalog.pg_roles WHERE rolname=current_user"
            )
            role = cursor.fetchone()
            if not role or role[0] != target["user"] or role[1] is not True:
                raise NativePostgresError(
                    "DUMB's configured PostgreSQL role cannot own this destination."
                )
            database_oid = _database(cursor, target["dbname"], target["user"])
            if database_oid is None:
                if role[2] is not True:
                    raise NativePostgresError(
                        "DUMB's configured PostgreSQL role needs permission to create databases."
                    )
                cursor.execute(
                    sql.SQL(
                        "CREATE DATABASE {} WITH OWNER {} TEMPLATE template0 "
                        "ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' LOCALE_PROVIDER libc"
                    ).format(
                        sql.Identifier(target["dbname"]), sql.Identifier(target["user"])
                    )
                )
                created = True
                database_oid = _database(cursor, target["dbname"], target["user"])
            _require_pristine(target, connect)
            identity = manager.target_inspector(target, require_empty=True)
            _require_collation(identity)
            if (
                identity.get("system_identifier") != system
                or identity.get("database_oid") != database_oid
                or identity.get("owner") != target["user"]
            ):
                raise NativePostgresError(
                    "The prepared database identity changed; DUMB retained it for review."
                )
    except psycopg2.Error:
        raise NativePostgresError(
            "DUMB could not prepare the managed destination. Check PostgreSQL availability "
            "and its configured role's create-database and inspection permissions. "
            "Any database already created was retained; no existing database was changed."
        ) from None
    finally:
        if connection is not None:
            connection.close()  # Closing also releases this session's advisory lock.
    return {
        "protocol": 1,
        "ready": True,
        "created": created,
        "target": {
            "host": target["host"],
            "port": target["port"],
            "database": target["dbname"],
        },
        "target_identity": {
            key: identity[key]
            for key in ("system_identifier", "database_oid", "schema")
        },
        "message": (
            "DUMB created the empty PostgreSQL database. Review the destination before copying."
            if created
            else "The existing empty PostgreSQL database is ready for review."
        ),
    }
