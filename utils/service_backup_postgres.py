"""Selected-service PostgreSQL dumps and identity-bound restore transactions.

The caller owns application quiescence and its durable installation journal.
Every mutation is preceded by ``persist(transaction)``; a restart may roll a
transaction back, but never resumes a half-restored scratch database.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import time
from urllib.parse import unquote, urlsplit

import psycopg2
from psycopg2 import sql

from utils.arr_postgres import (
    ARR_POSTGRES_KEYS,
    arr_postgres_config_file_enabled,
    arr_postgres_database_names,
    read_arr_postgres_xml_values,
)
from utils.database_health import (
    _parse_ado_net_connection_string,
    _provider_for_service,
    _service_postgres_connection,
)
from utils.installation_backup_archive import InstallationArchiveError, open_leaf
from utils.service_postgres import SERVICE_POSTGRES_KEYS, service_postgres_database_name


class ServicePostgresError(InstallationArchiveError):
    """A safe operator-facing failure without SQL, DSNs or credentials."""


_FIXED = {
    "riven_backend": "riven",
    "zilean": "zilean",
    "mediastorm": "mediastorm",
    "authelia": "authelia",
    "traefik_proxy_admin": "traefik_proxy_admin",
}
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_PROTECTED = {"postgres", "template0", "template1"}
_FORMAT = "dumb.service-postgres"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,62}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_JOB = re.compile(r"[A-Za-z0-9_-]{8,64}\Z")
_LOCK = 0x44554D4253425047
_OWNERSHIP_CATALOGS = (
    ("pg_class", "relnamespace", "relowner"),
    ("pg_proc", "pronamespace", "proowner"),
    ("pg_type", "typnamespace", "typowner"),
    ("pg_collation", "collnamespace", "collowner"),
    ("pg_conversion", "connamespace", "conowner"),
    ("pg_operator", "oprnamespace", "oprowner"),
    ("pg_opclass", "opcnamespace", "opcowner"),
    ("pg_opfamily", "opfnamespace", "opfowner"),
    ("pg_ts_dict", "dictnamespace", "dictowner"),
    ("pg_ts_config", "cfgnamespace", "cfgowner"),
    ("pg_statistic_ext", "stxnamespace", "stxowner"),
)


def _database_name(value):
    if (
        not isinstance(value, str)
        or not _IDENTIFIER.fullmatch(value)
        or value.lower() in _PROTECTED
        or value.startswith("dumb_sb_")
    ):
        raise ServicePostgresError(
            "The selected service has an unsupported or protected database name."
        )
    return value


def _entries(config):
    for key, section in config.items():
        if not isinstance(section, dict):
            continue
        if isinstance(section.get("instances"), dict):
            for instance_name, service in section["instances"].items():
                if isinstance(service, dict):
                    yield key, instance_name, service
        elif section.get("process_name"):
            yield key, None, section


def _endpoint(config):
    postgres = config.get("postgres") or {}
    if postgres.get("enabled") is not True:
        raise ServicePostgresError(
            "Start DUMB's managed PostgreSQL before backing up this service."
        )
    host = str(postgres.get("host") or "127.0.0.1").strip().lower()
    try:
        port = int(postgres.get("port") or 5432)
    except (TypeError, ValueError):
        raise ServicePostgresError("The managed PostgreSQL port is invalid.") from None
    if host not in _LOOPBACK or not 1 <= port <= 65535:
        raise ServicePostgresError(
            "Individual-service backups support only DUMB's local managed PostgreSQL."
        )
    return {
        "host": host,
        "port": port,
        "user": str(postgres.get("user") or "DUMB"),
        "password": str(postgres.get("password") or "postgres"),
    }


def _connection_target(connection, managed):
    """Compare actual runtime selectors without returning any secret fields."""
    values = dict(connection or {})
    if values.get("dsn"):
        try:
            parsed = urlsplit(str(values["dsn"]))
            if parsed.scheme not in {"postgres", "postgresql", "postgresql+psycopg2"}:
                raise ValueError
            # Libpq URL parameters can override the apparent host or dbname.
            if parsed.query or parsed.fragment:
                raise ValueError
            values = {
                "host": parsed.hostname,
                "port": parsed.port,
                "user": unquote(parsed.username or ""),
                "password": unquote(parsed.password or ""),
                "database": unquote(parsed.path.removeprefix("/")),
            }
        except (ValueError, TypeError):
            raise ServicePostgresError(
                "The service's PostgreSQL connection cannot be safely matched to DUMB."
            ) from None
    try:
        host = str(values.get("host") or managed["host"]).lower()
        port = int(values.get("port") or managed["port"])
    except (ValueError, TypeError):
        raise ServicePostgresError(
            "The service's PostgreSQL endpoint is invalid."
        ) from None
    if (
        host not in _LOOPBACK
        or host != managed["host"]
        or port != managed["port"]
        or values.get("user") not in (None, "", managed["user"])
        or values.get("password") not in (None, "", managed["password"])
    ):
        raise ServicePostgresError(
            "This service uses an external database or separate credentials; "
            "individual-service backup cannot include it."
        )
    return values.get("database")


def _specifications(config, key, instance_name, service):
    provider = _provider_for_service(key, service)
    if key in ARR_POSTGRES_KEYS and arr_postgres_config_file_enabled(service):
        provider = "postgresql"
    if key in SERVICE_POSTGRES_KEYS and service.get("postgres_enabled") is True:
        provider = "postgresql"
    if provider == "unknown":
        raise ServicePostgresError("The service's database selection is unresolved.")
    if provider != "postgresql" or key == "pgadmin":
        return []
    if key == "infinidysk":
        if str(service.get("runtime_generation") or "1") != "2":
            raise ServicePostgresError(
                "InfiniDysk 1 PostgreSQL uses installation-bound migration authorization; "
                "use a whole-installation backup and restore for this service."
            )
        from utils.infinidysk_v2_runtime import _native_database_boot

        try:
            _data, boot = _native_database_boot(
                service, config.get("data_root") or "/data"
            )
        except (OSError, ValueError):
            raise ServicePostgresError(
                "InfiniDysk's native database boot selection is unavailable or unsafe."
            ) from None
        if boot.get("migration"):
            raise ServicePostgresError(
                "Finish InfiniDysk's native database migration before backing up this service."
            )
    managed = _endpoint(config)
    if key in ARR_POSTGRES_KEYS:
        values = read_arr_postgres_xml_values(service)
        _connection_target(
            {
                "host": values.get("PostgresHost"),
                "port": values.get("PostgresPort"),
                "user": values.get("PostgresUser"),
                "password": values.get("PostgresPassword"),
            },
            managed,
        )
        names = arr_postgres_database_names(key, instance_name or "Default", service)
        if any(
            values.get(tag) and values[tag] != name
            for tag, name in zip(("PostgresMainDb", "PostgresLogDb"), names)
        ):
            raise ServicePostgresError(
                "The Arr database names differ between DUMB and the running service configuration."
            )
        return [
            {"database": _database_name(name), "role": role}
            for role, name in zip(("main", "logs"), names)
        ]
    if key in _FIXED:
        env = service.get("env") or {}
        connection = {}
        if key == "riven_backend":
            connection["dsn"] = env.get("RIVEN_DATABASE_URL") or env.get(
                "RIVEN_DATABASE_HOST"
            )
        elif key in {"mediastorm", "traefik_proxy_admin"}:
            connection["dsn"] = env.get("DATABASE_URL")
        elif key == "zilean":
            value = str(env.get("Zilean__Database__ConnectionString") or "")
            if "{postgres_" not in value:
                connection = _parse_ado_net_connection_string(value)
        name = _connection_target(connection, managed) or _FIXED[key]
        if name != _FIXED[key]:
            raise ServicePostgresError(
                "This service's custom database name has no managed backup contract."
            )
        return [{"database": _database_name(name), "role": "main"}]
    if key not in {*SERVICE_POSTGRES_KEYS, "aiostreams"}:
        raise ServicePostgresError(
            "This PostgreSQL service has no individual-service backup adapter."
        )
    connection = _service_postgres_connection(key, service, config)
    if (
        key in SERVICE_POSTGRES_KEYS
        and str(service.get("runtime_generation") or "1") != "2"
    ):
        canonical = service_postgres_database_name(key, instance_name, service)
        # Native selectors take precedence once setup has actually written them.
        # An unconfigured/disabled instance still owns its declared database.
        env = service.get("env") or {}
        configured_runtime = any(
            env.get(field)
            for field in (
                "DATABASE_CONNECTION_STRING",
                "POSTGRES_DATABASE",
                "dbName",
                "dbConnectionString",
                "DB_NAME",
            )
        )
        if not configured_runtime and not connection.get("dsn"):
            connection["database"] = canonical
    name = _connection_target(connection, managed)
    return [{"database": _database_name(name or key), "role": "main"}]


def _unqualified_local_names(config, key, instance_name, service):
    """Retain competing ownership even when that service's credentials differ."""
    if key in ARR_POSTGRES_KEYS and (
        service.get("postgres_enabled") is True
        or arr_postgres_config_file_enabled(service)
    ):
        values = read_arr_postgres_xml_values(service)
        if str(values.get("PostgresHost") or "127.0.0.1").lower() in _LOOPBACK:
            return arr_postgres_database_names(key, instance_name or "Default", service)
        return []
    if key not in {*SERVICE_POSTGRES_KEYS, "aiostreams", *_FIXED}:
        return []
    if (
        _provider_for_service(key, service) != "postgresql"
        and service.get("postgres_enabled") is not True
    ):
        return []
    connection = _service_postgres_connection(key, service, config)
    try:
        if connection.get("dsn"):
            parsed = urlsplit(str(connection["dsn"]))
            host = parsed.hostname
            port = parsed.port or 5432
            database = unquote(parsed.path.removeprefix("/"))
        else:
            host = connection.get("host") or "127.0.0.1"
            port = int(connection.get("port") or 5432)
            database = connection.get("database")
        managed_port = int((config.get("postgres") or {}).get("port") or 5432)
        if str(host).lower() in _LOOPBACK and port == managed_port:
            return [
                database or service_postgres_database_name(key, instance_name, service)
            ]
    except (ValueError, TypeError):
        pass
    return []


def service_databases(config, key, instance_name=None):
    """Return selected logical databases, rejecting shared/ambiguous ownership.

    Disabled instances still own their retained databases. A PostgreSQL database
    used by another configured instance cannot be restored independently.
    """
    selected = None
    owners = {}
    for candidate_key, candidate_instance, service in _entries(config):
        try:
            specs = _specifications(config, candidate_key, candidate_instance, service)
        except ServicePostgresError:
            if (candidate_key, candidate_instance) == (key, instance_name):
                raise
            for name in _unqualified_local_names(
                config, candidate_key, candidate_instance, service
            ):
                owners.setdefault(name, []).append((candidate_key, candidate_instance))
            continue
        owner = (candidate_key, candidate_instance)
        if owner == (key, instance_name):
            selected = specs
        for spec in specs:
            owners.setdefault(spec["database"], []).append(owner)
    if selected is None:
        raise ServicePostgresError("The selected service instance no longer exists.")
    for spec in selected:
        if owners.get(spec["database"]) != [(key, instance_name)]:
            raise ServicePostgresError(
                "A selected database is shared with another configured service or "
                "instance; use a whole-installation backup instead."
            )
    return selected


def _private_directory(path):
    path = Path(path)
    if not path.is_absolute():
        raise ServicePostgresError(
            "Database artifacts require an absolute local directory."
        )
    for ancestor in [*reversed(path.parents), path]:
        if ancestor.is_symlink():
            raise ServicePostgresError(
                "Database artifact directories cannot use symlinks."
            )
    path.mkdir(mode=0o700, exist_ok=True)
    value = path.lstat()
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid()
        or stat.S_IMODE(value.st_mode) != 0o700
    ):
        raise ServicePostgresError(
            "Database artifact storage is not controller-private."
        )
    return path


def _descriptor_proof(descriptor):
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != os.geteuid()
        or before.st_mode & 0o077
    ):
        raise ServicePostgresError("A PostgreSQL dump is not a private regular file.")
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    after = os.fstat(descriptor)
    if _file_signature(before) != _file_signature(after):
        raise ServicePostgresError("A PostgreSQL dump changed while being checked.")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return {"bytes": before.st_size, "sha256": digest.hexdigest()}


def _file_signature(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _file_proof(path):
    with open_leaf(path) as (parent, leaf):
        descriptor = os.open(
            leaf, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent
        )
        try:
            proof = _descriptor_proof(descriptor)
            named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if _file_signature(os.fstat(descriptor)) != _file_signature(named):
                raise ServicePostgresError(
                    "A PostgreSQL dump changed while being checked."
                )
            return proof
        finally:
            os.close(descriptor)


class PostgresServiceBackup:
    """Adapter for a caller-managed, quiesced selected-service operation."""

    def __init__(self, config, *, connect=None, run=None):
        self.config = config
        self.params = _endpoint(config)
        self.connect = connect or psycopg2.connect
        self.run = run

    @contextmanager
    def _connection(self, database="postgres", **overrides):
        connection = None
        try:
            connection = self.connect(
                **{
                    **self.params,
                    "dbname": database,
                    "connect_timeout": 5,
                    "application_name": "dumb-service-backup",
                    **overrides,
                }
            )
            connection.autocommit = True
            yield connection
        except psycopg2.Error:
            raise ServicePostgresError(
                "The selected PostgreSQL database operation failed; "
                "retained recovery evidence was preserved."
            ) from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _one(connection, query, parameters=()):
        with connection.cursor() as cursor:
            cursor.execute(query, parameters)
            return cursor.fetchone()

    @staticmethod
    def _all(connection, query, parameters=()):
        with connection.cursor() as cursor:
            cursor.execute(query, parameters)
            return cursor.fetchall()

    @staticmethod
    def _execute(connection, query, parameters=()):
        with connection.cursor() as cursor:
            cursor.execute(query, parameters)

    @contextmanager
    def _locked(self, expected=None):
        with self._connection() as connection:
            if not self._one(connection, "SELECT pg_try_advisory_lock(%s)", (_LOCK,))[
                0
            ]:
                raise ServicePostgresError(
                    "Another service database operation is running."
                )
            try:
                cluster = self._cluster(connection)
                if expected is not None and cluster != expected:
                    raise ServicePostgresError(
                        "The managed PostgreSQL cluster changed; no database was replaced."
                    )
                yield connection, cluster
            finally:
                self._execute(connection, "SELECT pg_advisory_unlock(%s)", (_LOCK,))

    def _cluster(self, connection):
        row = self._one(
            connection,
            "SELECT system_identifier::text, current_setting('server_version_num')::int, "
            "current_setting('data_directory') FROM pg_control_system()",
        )
        data = str(
            (self.config.get("postgres") or {}).get("config_dir") or "/postgres_data"
        )
        if not row or os.path.realpath(str(row[2])) != os.path.realpath(data):
            raise ServicePostgresError(
                "The PostgreSQL endpoint is not DUMB's configured managed data directory."
            )
        major = int(row[1]) // 10000
        if major < 15 or not str(row[0]).isdigit():
            raise ServicePostgresError(
                "Individual-service logical backup requires managed PostgreSQL 15 or newer."
            )
        return {
            "system_identifier": str(row[0]),
            "major": major,
            "data_directory": data,
        }

    def _database(self, connection, name):
        row = self._one(
            connection,
            "SELECT d.oid::text, d.datname, d.datdba::text, r.rolname, "
            "pg_encoding_to_char(d.encoding), d.datcollate, d.datctype, "
            "d.datlocprovider, d.daticulocale, d.datcollversion, d.datallowconn, "
            "d.datconnlimit, d.datistemplate, shobj_description(d.oid, 'pg_database'), "
            "d.datacl::text, t.spcname FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba "
            "JOIN pg_tablespace t ON t.oid=d.dattablespace "
            "WHERE d.datname=%s",
            (name,),
        )
        if row is None:
            return None
        fields = (
            "oid",
            "name",
            "owner_oid",
            "owner",
            "encoding",
            "collate",
            "ctype",
            "locale_provider",
            "icu_locale",
            "collation_version",
            "allow_connections",
            "connection_limit",
            "template",
            "comment",
            "acl",
            "tablespace",
        )
        return dict(zip(fields, row))

    def _tool(self, program, major):
        path = Path("/usr/lib/postgresql") / str(major) / "bin" / program
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ServicePostgresError(
                "The matching PostgreSQL dump/restore tools are not installed."
            )
        return str(path)

    def _command(
        self,
        program,
        major,
        database,
        arguments,
        *,
        credentials=None,
        stdout=None,
        stdin=None,
        progress=None,
    ):
        values = credentials or self.params
        env = {
            key: value for key, value in os.environ.items() if not key.startswith("PG")
        }
        env.update(
            PGHOST=self.params["host"],
            PGPORT=str(self.params["port"]),
            PGUSER=values["user"],
            PGPASSWORD=values["password"],
            PGDATABASE=database,
            PGCONNECT_TIMEOUT="5",
            PGAPPNAME="dumb-service-backup",
        )
        try:
            command = [self._tool(program, major), *arguments]
            if program == "pg_restore":
                # Unlike pg_dump, pg_restore needs an explicit -d to select
                # connected restore mode; PGDATABASE alone selects no target.
                command.append("--dbname=" + database)
            options = {
                "env": env,
                "stdin": stdin if stdin is not None else subprocess.DEVNULL,
                "stdout": stdout if stdout is not None else subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if self.run is not None:
                result = self.run(command, **options, timeout=6 * 60 * 60, check=False)
                returncode = result.returncode
            else:
                process = subprocess.Popen(command, **options, start_new_session=True)
                try:
                    deadline = time.monotonic() + 6 * 60 * 60
                    while process.poll() is None:
                        if time.monotonic() >= deadline:
                            raise ServicePostgresError(
                                "The PostgreSQL helper exceeded its operation time limit."
                            )
                        if progress:
                            sample = {
                                "phase": (
                                    "database_dump"
                                    if program == "pg_dump"
                                    else "database_restore"
                                )
                            }
                            if stdout is not None:
                                sample["bytes"] = os.fstat(stdout).st_size
                            progress(sample)
                        time.sleep(0.5)
                    returncode = process.wait()
                finally:
                    # Cancellation/error callbacks must leave no native writer
                    # behind before filesystem or database rollback starts.
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)
                    if process.poll() is None:
                        raise ServicePostgresError(
                            "The PostgreSQL helper did not stop; retained recovery is required."
                        )
        except (OSError, subprocess.SubprocessError):
            raise ServicePostgresError(
                "The PostgreSQL dump/restore helper did not finish successfully."
            ) from None
        if returncode != 0:
            raise ServicePostgresError(
                "The PostgreSQL dump/restore helper rejected this database. "
                "Check that its extensions support an unprivileged restore."
            )

    def _census(self, database, progress=None, *, credentials=None):
        overrides = credentials or {}
        with self._connection(database, **overrides) as connection:
            tables = self._all(
                connection,
                "SELECT n.nspname, c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.relkind IN ('r','m','p') AND n.nspname !~ '^pg_' "
                "AND n.nspname <> 'information_schema' ORDER BY 1,2",
            )
            if len(tables) > 4096:
                raise ServicePostgresError(
                    "The selected database exceeds the bounded restore record inventory."
                )
            counts = []
            for index, (schema, table) in enumerate(tables):
                count = self._one(
                    connection,
                    sql.SQL("SELECT count(*) FROM ONLY {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(table)
                    ),
                )[0]
                counts.append([schema, table, count])
                if progress:
                    progress(
                        {
                            "phase": "database_records",
                            "completed": index + 1,
                            "total": len(tables),
                        }
                    )
            extensions = self._all(
                connection,
                "SELECT extname, extversion FROM pg_extension ORDER BY extname",
            )
            if len(extensions) > 128:
                raise ServicePostgresError(
                    "The selected database exceeds the bounded extension inventory."
                )
            return {"tables": counts, "extensions": [list(row) for row in extensions]}

    def backup(self, specs, directory, progress=None):
        directory = _private_directory(directory)
        result = {"format": _FORMAT, "version": 1, "databases": []}
        with self._locked() as (connection, cluster):
            result["cluster"] = cluster
            for index, spec in enumerate(specs):
                name = _database_name(spec["database"])
                database = self._database(connection, name)
                if database is None or database["owner"] != self.params["user"]:
                    raise ServicePostgresError(
                        "A selected database is missing or has an unmanaged owner."
                    )
                self._require_supported(connection, database)
                self._require_idle(connection, database["oid"])
                census = self._census(name, progress)
                path = directory / f"{index:03d}.dump"
                descriptor = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                try:
                    self._command(
                        "pg_dump",
                        cluster["major"],
                        name,
                        ["--format=custom", "--no-owner", "--no-acl"],
                        stdout=descriptor,
                        progress=progress,
                    )
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._require_idle(connection, database["oid"])
                if (
                    census != self._census(name, progress)
                    or self._database(connection, name) != database
                ):
                    raise ServicePostgresError(
                        "The stopped service's PostgreSQL database changed during backup."
                    )
                proof = _file_proof(path)
                result["databases"].append(
                    {
                        "database": name,
                        "role": spec["role"],
                        "file": path.name,
                        **proof,
                        "identity": database,
                        "census": census,
                    }
                )
                if progress:
                    progress(
                        {
                            "phase": "database_dump",
                            "completed": index + 1,
                            "total": len(specs),
                            "bytes": proof["bytes"],
                        }
                    )
        return result

    def estimate(self, specs):
        """Read-only readiness and uncompressed database-size estimate."""
        if not specs:
            return 0
        total = 0
        names = set()
        with self._locked() as (connection, cluster):
            self._tool("pg_dump", cluster["major"])
            self._tool("pg_restore", cluster["major"])
            for spec in specs:
                name = _database_name(spec["database"])
                if name in names:
                    raise ServicePostgresError(
                        "A selected database was listed more than once."
                    )
                names.add(name)
                database = self._database(connection, name)
                if database is None or database["owner"] != self.params["user"]:
                    raise ServicePostgresError(
                        "A selected database is missing or has an unmanaged owner."
                    )
                self._require_supported(connection, database)
                total += int(
                    self._one(connection, "SELECT pg_database_size(%s)", (name,))[0]
                )
        return total

    def _require_idle(self, connection, oid):
        # libpq close can return just before its server process exits. Permit
        # that bounded drain; never terminate sessions or wait indefinitely.
        deadline = time.monotonic() + 2
        while self._one(
            connection,
            "SELECT count(*) FROM pg_stat_activity WHERE datid=%s::oid",
            (oid,),
        )[0]:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        else:
            deadline = None
        if deadline is not None:
            raise ServicePostgresError(
                "A selected database still has active connections. Pause its writers "
                "and close database clients before retrying; no sessions were terminated."
            )
        if self._one(
            connection,
            "SELECT count(*) FROM pg_prepared_xacts WHERE database=(SELECT datname FROM pg_database WHERE oid=%s::oid)",
            (oid,),
        )[0]:
            raise ServicePostgresError(
                "A selected database has unresolved prepared transactions."
            )

    def _require_supported(self, connection, database):
        if database["template"] or database["tablespace"] != "pg_default":
            raise ServicePostgresError(
                "Custom database templates or tablespaces require a whole-installation backup."
            )
        if database["acl"] is not None:
            actual = {
                tuple(row)
                for row in self._all(
                    connection,
                    "SELECT a.grantee::text, a.privilege_type, a.is_grantable, a.grantor::text "
                    "FROM pg_database d CROSS JOIN LATERAL aclexplode(d.datacl) a "
                    "WHERE d.oid=%s::oid",
                    (database["oid"],),
                )
            }
            owner = database["owner_oid"]
            expected = {
                (owner, permission, False, owner)
                for permission in ("CREATE", "CONNECT", "TEMPORARY")
            } | {
                ("0", permission, False, owner)
                for permission in ("CONNECT", "TEMPORARY")
            }
            if actual != expected:
                raise ServicePostgresError(
                    "Custom database-level permissions require a whole-installation backup."
                )
        if self._one(
            connection,
            "SELECT count(*) FROM pg_db_role_setting WHERE setdatabase=%s::oid",
            (database["oid"],),
        )[0]:
            raise ServicePostgresError(
                "Custom database/role session settings require a whole-installation backup."
            )
        with self._connection(database["name"]) as selected:
            # --no-owner/--no-acl is safe only for DUMB's ordinary managed
            # ownership/permission contract. Reject other owners and ACLs
            # instead of silently flattening them during restricted restore.
            ownership = sql.SQL(" UNION ALL ").join(
                sql.SQL(
                    "SELECT c.{} AS owner FROM {} c JOIN pg_namespace n "
                    "ON n.oid=c.{} WHERE n.nspname !~ '^pg_' "
                    "AND n.nspname <> 'information_schema'"
                ).format(
                    sql.Identifier(owner),
                    sql.Identifier(catalog),
                    sql.Identifier(namespace),
                )
                for catalog, namespace, owner in _OWNERSHIP_CATALOGS
            )
            ownership += sql.SQL(
                " UNION ALL SELECT nspowner AS owner FROM pg_namespace "
                "WHERE nspname !~ '^pg_' AND nspname NOT IN ('information_schema','public') "
                "UNION ALL SELECT lomowner AS owner FROM pg_largeobject_metadata "
                "UNION ALL SELECT extowner AS owner FROM pg_extension WHERE extname<>'plpgsql'"
            )
            if self._one(
                selected,
                sql.SQL("SELECT count(*) FROM ({}) owners WHERE owner<>%s::oid").format(
                    ownership
                ),
                (database["owner_oid"],),
            )[0]:
                raise ServicePostgresError(
                    "This database contains custom object ownership; use a whole-installation backup."
                )
            unsupported = self._one(
                selected,
                "SELECT (SELECT count(*) FROM pg_foreign_server) + "
                "(SELECT count(*) FROM pg_subscription) + (SELECT count(*) FROM pg_event_trigger) + "
                "(SELECT count(*) FROM pg_extension e LEFT JOIN pg_available_extension_versions v "
                "ON v.name=e.extname AND v.version=e.extversion WHERE v.name IS NULL OR (v.superuser AND NOT v.trusted)) + "
                "(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
                "AND (c.relacl IS NOT NULL OR c.reltablespace <> 0)) + "
                "(SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND p.proacl IS NOT NULL) + "
                "(SELECT count(*) FROM pg_namespace n WHERE n.nspname !~ '^pg_' "
                "AND n.nspname NOT IN ('information_schema','public') AND n.nspacl IS NOT NULL) + "
                "(SELECT count(*) FROM pg_default_acl) + "
                "(SELECT count(*) FROM pg_largeobject_metadata WHERE lomacl IS NOT NULL) + "
                "(SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
                "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' AND t.typacl IS NOT NULL) + "
                "(SELECT count(*) FROM pg_foreign_data_wrapper) + "
                "(SELECT count(*) FROM pg_publication) + "
                "(SELECT count(*) FROM pg_language WHERE lanname NOT IN ('internal','c','sql','plpgsql') OR lanowner<>10) + "
                "(SELECT count(*) FROM pg_extension WHERE extname='plpgsql' AND extowner<>10)",
            )[0]
            if unsupported:
                raise ServicePostgresError(
                    "This database has custom object permissions, an unavailable/untrusted extension "
                    "or an external database object; use a whole-installation backup."
                )
            public = self._all(
                selected,
                "SELECT pg_get_userbyid(n.nspowner), a.grantee::text, a.privilege_type, "
                "a.is_grantable, a.grantor::text FROM pg_namespace n "
                "CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl,acldefault('n',n.nspowner))) a "
                "WHERE n.nspname='public'",
            )
            owner = self._one(
                selected,
                "SELECT oid::text FROM pg_roles WHERE rolname='pg_database_owner'",
            )[0]
            expected = {
                ("pg_database_owner", owner, permission, False, owner)
                for permission in ("USAGE", "CREATE")
            } | {("pg_database_owner", "0", "USAGE", False, owner)}
            if {tuple(row) for row in public} != expected:
                raise ServicePostgresError(
                    "Custom public-schema permissions require a whole-installation backup."
                )

    @staticmethod
    def _manifest(value):
        if (
            not isinstance(value, dict)
            or value.get("format") != _FORMAT
            or type(value.get("version")) is not int
            or value.get("version") != 1
            or not isinstance(value.get("cluster"), dict)
            or not isinstance(value.get("databases"), list)
            or not 1 <= len(value["databases"]) <= 2
        ):
            raise ServicePostgresError(
                "The selected-service PostgreSQL manifest is invalid."
            )
        names = set()
        for index, row in enumerate(value["databases"]):
            if not isinstance(row, dict):
                raise ServicePostgresError(
                    "The PostgreSQL database manifest is invalid."
                )
            name = _database_name(row.get("database"))
            identity = row.get("identity")
            census = row.get("census")
            if (
                name in names
                or row.get("role") not in {"main", "logs"}
                or row.get("file") != f"{index:03d}.dump"
                or not isinstance(row.get("bytes"), int)
                or isinstance(row["bytes"], bool)
                or row["bytes"] < 1
                or not isinstance(row.get("sha256"), str)
                or not _HASH.fullmatch(row["sha256"])
                or not isinstance(identity, dict)
                or identity.get("name") != name
                or not isinstance(census, dict)
                or not isinstance(census.get("tables"), list)
                or len(census["tables"]) > 4096
                or not isinstance(census.get("extensions"), list)
                or len(census["extensions"]) > 128
            ):
                raise ServicePostgresError(
                    "The PostgreSQL database manifest is invalid."
                )
            for field in ("encoding", "collate", "ctype", "owner"):
                if (
                    not isinstance(identity.get(field), str)
                    or not 1 <= len(identity[field]) <= 1024
                ):
                    raise ServicePostgresError(
                        "The archived PostgreSQL database settings are invalid."
                    )
            if identity.get("locale_provider") not in {"c", "i"} or (
                identity["locale_provider"] == "i"
                and not isinstance(identity.get("icu_locale"), str)
            ):
                raise ServicePostgresError(
                    "The archived PostgreSQL locale is unsupported."
                )
            if (
                not isinstance(identity.get("allow_connections"), bool)
                or type(identity.get("connection_limit")) is not int
                or not -1 <= identity["connection_limit"] <= 2147483647
                or (
                    identity.get("comment") is not None
                    and not isinstance(identity["comment"], str)
                )
                or identity.get("template") is not False
                or identity.get("tablespace") != "pg_default"
            ):
                raise ServicePostgresError(
                    "The archived PostgreSQL connection settings are invalid."
                )
            for table in census["tables"]:
                if (
                    not isinstance(table, list)
                    or len(table) != 3
                    or any(
                        not isinstance(name, str) or not 1 <= len(name.encode()) <= 63
                        for name in table[:2]
                    )
                    or type(table[2]) is not int
                    or table[2] < 0
                ):
                    raise ServicePostgresError(
                        "The archived PostgreSQL record inventory is invalid."
                    )
            names.add(name)
        return value

    def _server_binding(self):
        """Filesystem guard suitable for use before the stopped server starts."""
        raw = str(
            (self.config.get("postgres") or {}).get("config_dir") or "/postgres_data"
        )
        path = Path(raw).resolve(strict=True)
        value = path.lstat()
        if not stat.S_ISDIR(value.st_mode) or value.st_mode & 0o022:
            raise ServicePostgresError(
                "The managed PostgreSQL data directory is unsafe."
            )
        with open_leaf(path / "PG_VERSION") as (parent, leaf):
            descriptor = os.open(
                leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            try:
                version = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(version.st_mode)
                    or version.st_nlink != 1
                    or version.st_size > 32
                ):
                    raise ServicePostgresError(
                        "The managed PostgreSQL version marker is unsafe."
                    )
                content = os.read(descriptor, 33)
                if not re.fullmatch(rb"[0-9]+\s*", content):
                    raise ServicePostgresError(
                        "The managed PostgreSQL version marker is invalid."
                    )
                if _file_signature(version) != _file_signature(os.fstat(descriptor)):
                    raise ServicePostgresError(
                        "The managed PostgreSQL version marker changed."
                    )
            finally:
                os.close(descriptor)
        return {
            "path": str(path),
            "directory": [
                value.st_dev,
                value.st_ino,
                value.st_uid,
                value.st_gid,
                stat.S_IMODE(value.st_mode),
            ],
            "version": [
                version.st_dev,
                version.st_ino,
                hashlib.sha256(content).hexdigest(),
            ],
            "port": self.params["port"],
            "user": self.params["user"],
        }

    def verify_server_binding(self, transaction):
        transaction = self._transaction(transaction)
        try:
            actual = self._server_binding()
        except (OSError, ValueError):
            raise ServicePostgresError(
                "The retained PostgreSQL server files are unavailable or unsafe."
            ) from None
        if actual != transaction.get("server_binding"):
            raise ServicePostgresError(
                "The managed PostgreSQL server files changed; recovery cannot start this cluster."
            )
        return True

    def _role(self, connection, name):
        row = self._one(
            connection,
            "SELECT r.oid::text, shobj_description(r.oid,'pg_authid'), r.rolcanlogin, "
            "r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolinherit, r.rolreplication, "
            "r.rolbypassrls, EXISTS(SELECT 1 FROM pg_auth_members WHERE member=r.oid) "
            "FROM pg_roles r WHERE r.rolname=%s",
            (name,),
        )
        if row is None:
            return None
        return {"oid": row[0], "tag": row[1], "login": row[2], "elevated": any(row[3:])}

    def _role_matches(self, connection, role):
        actual = self._role(connection, role["name"])
        if (
            actual is None
            or actual["tag"] != role["tag"]
            or actual["elevated"]
            or (role.get("oid") is not None and actual["oid"] != role["oid"])
        ):
            raise ServicePostgresError(
                "The restore-owned PostgreSQL role identity changed."
            )
        return actual

    def prepare_restore(
        self, manifest, directory, job_id, persist, progress=None, *, specs=None
    ):
        """Restore into owned scratch databases without replacing live names.

        ``persist`` must retain this transaction even when this method raises.
        That retained transaction is sufficient for restart-safe rollback.
        """
        manifest = self._manifest(manifest)
        if not isinstance(job_id, str) or not _JOB.fullmatch(job_id):
            raise ServicePostgresError("The service restore job identity is invalid.")
        if (
            specs is not None
            and [
                {"database": row["database"], "role": row["role"]}
                for row in manifest["databases"]
            ]
            != specs
        ):
            raise ServicePostgresError(
                "The archive does not match this service's current databases."
            )
        directory = _private_directory(directory)
        for row in manifest["databases"]:
            if _file_proof(directory / row["file"]) != {
                "bytes": row["bytes"],
                "sha256": row["sha256"],
            }:
                raise ServicePostgresError(
                    "A staged PostgreSQL dump failed complete checksum verification."
                )
        nonce = secrets.token_hex(16)
        prefix = "dumb_sb_" + nonce
        transaction = {
            "format": _FORMAT,
            "version": 1,
            "job_id": job_id,
            "nonce": nonce,
            "cluster": manifest["cluster"],
            "phase": "preparing",
            "server_binding": self._server_binding(),
            "role": {
                "name": prefix + "_role",
                "oid": None,
                "tag": _FORMAT + ":" + nonce,
            },
            "databases": [],
        }
        with self._locked(manifest["cluster"]) as (connection, cluster):
            for index, row in enumerate(manifest["databases"]):
                current = self._database(connection, row["database"])
                if (
                    current is None
                    or current["owner"] != self.params["user"]
                    or row["identity"]["owner"] != self.params["user"]
                ):
                    raise ServicePostgresError(
                        "The service database is missing or its configured owner changed."
                    )
                self._require_supported(connection, current)
                self._require_idle(connection, current["oid"])
                transaction["databases"].append(
                    {
                        "target": row["database"],
                        "scratch": f"{prefix}_{index}_new",
                        "retained": f"{prefix}_{index}_old",
                        "original": current,
                        "new_oid": None,
                        "new_tag": f"{_FORMAT}:{nonce}:{index}:new",
                        "old_tag": f"{_FORMAT}:{nonce}:{index}:old",
                        "settings": row["identity"],
                        "ready": False,
                    }
                )
            persist(transaction)
            role = transaction["role"]
            password = secrets.token_urlsafe(32)
            if self._role(connection, role["name"]) is not None:
                raise ServicePostgresError(
                    "An unrecognized restore role already exists."
                )
            connection.autocommit = False
            try:
                self._execute(
                    connection,
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %s"
                    ).format(sql.Identifier(role["name"])),
                    (password,),
                )
                self._execute(
                    connection,
                    sql.SQL("COMMENT ON ROLE {} IS %s").format(
                        sql.Identifier(role["name"])
                    ),
                    (role["tag"],),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.autocommit = True
            role["oid"] = self._role_matches(connection, role)["oid"]
            persist(transaction)
            credentials = {"user": role["name"], "password": password}
            for record, dump in zip(transaction["databases"], manifest["databases"]):
                self._create_scratch(connection, record, role)
                persist(transaction)
                path = directory / dump["file"]
                with open_leaf(path) as (parent, leaf):
                    descriptor = os.open(
                        leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                    )
                    try:
                        # pg_restore reads the inherited descriptor, never a
                        # second path that could be replaced after validation.
                        proof = _descriptor_proof(descriptor)
                        if proof != {"bytes": dump["bytes"], "sha256": dump["sha256"]}:
                            raise ServicePostgresError(
                                "A staged PostgreSQL dump changed before restore."
                            )
                        self._command(
                            "pg_restore",
                            cluster["major"],
                            record["scratch"],
                            [
                                "--single-transaction",
                                "--exit-on-error",
                                "--no-owner",
                                "--no-acl",
                            ],
                            credentials=credentials,
                            stdin=descriptor,
                            progress=progress,
                        )
                    finally:
                        os.close(descriptor)
                if (
                    self._census(record["scratch"], progress, credentials=credentials)
                    != dump["census"]
                ):
                    raise ServicePostgresError(
                        "The restored database does not match the archived record inventory."
                    )
                self._require_idle(connection, record["new_oid"])
                with self._connection(record["scratch"]) as scratch:
                    self._execute(
                        scratch,
                        sql.SQL("REASSIGN OWNED BY {} TO {}").format(
                            sql.Identifier(role["name"]),
                            sql.Identifier(record["original"]["owner"]),
                        ),
                    )
                self._execute(
                    connection,
                    sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                        sql.Identifier(record["scratch"]),
                        sql.Identifier(record["original"]["owner"]),
                    ),
                )
                record["ready"] = True
                persist(transaction)
            self._drop_role(connection, role)
            transaction["phase"] = "prepared"
            persist(transaction)
        return transaction

    def _create_scratch(self, connection, record, role):
        settings = record["settings"]
        if (
            self._database(connection, record["scratch"]) is not None
            or self._database(connection, record["retained"]) is not None
        ):
            raise ServicePostgresError(
                "An unrecognized restore database already exists."
            )
        query = sql.SQL(
            "CREATE DATABASE {} WITH TEMPLATE template0 OWNER {} ENCODING %s LC_COLLATE %s LC_CTYPE %s LOCALE_PROVIDER "
        ).format(sql.Identifier(record["scratch"]), sql.Identifier(role["name"]))
        values = [settings["encoding"], settings["collate"], settings["ctype"]]
        if settings["locale_provider"] == "i":
            query += sql.SQL("icu ICU_LOCALE %s")
            values.append(settings["icu_locale"])
        else:
            query += sql.SQL("libc")
        self._execute(connection, query, values)
        database = self._database(connection, record["scratch"])
        record["new_oid"] = database["oid"]
        self._execute(
            connection,
            sql.SQL("COMMENT ON DATABASE {} IS %s").format(
                sql.Identifier(record["scratch"])
            ),
            (record["new_tag"],),
        )
        self._execute(
            connection,
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(record["scratch"])
            ),
        )

    def _drop_role(self, connection, role):
        if self._role(connection, role["name"]) is None:
            return
        self._role_matches(connection, role)
        self._execute(
            connection,
            sql.SQL("ALTER ROLE {} NOLOGIN").format(sql.Identifier(role["name"])),
        )
        self._execute(
            connection, sql.SQL("DROP ROLE {} ").format(sql.Identifier(role["name"]))
        )

    @staticmethod
    def _transaction(value):
        if (
            not isinstance(value, dict)
            or value.get("format") != _FORMAT
            or type(value.get("version")) is not int
            or value.get("version") != 1
            or not isinstance(value.get("nonce"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", value["nonce"])
            or not isinstance(value.get("databases"), list)
            or not 1 <= len(value["databases"]) <= 2
            or value.get("phase")
            not in {
                "preparing",
                "prepared",
                "applying",
                "applied",
                "rolling_back",
                "rolled_back",
                "finishing",
                "finished",
            }
        ):
            raise ServicePostgresError(
                "The retained PostgreSQL restore journal is invalid."
            )
        prefix = "dumb_sb_" + value["nonce"]
        role = value.get("role") or {}
        if (
            role.get("name") != prefix + "_role"
            or role.get("tag") != _FORMAT + ":" + value["nonce"]
        ):
            raise ServicePostgresError(
                "The retained PostgreSQL role binding is invalid."
            )
        names = set()
        for index, record in enumerate(value["databases"]):
            name = _database_name(record.get("target"))
            if (
                name in names
                or record.get("scratch") != f"{prefix}_{index}_new"
                or record.get("retained") != f"{prefix}_{index}_old"
                or record.get("new_tag") != f"{_FORMAT}:{value['nonce']}:{index}:new"
                or record.get("old_tag") != f"{_FORMAT}:{value['nonce']}:{index}:old"
                or not isinstance(record.get("original"), dict)
                or record["original"].get("name") != name
                or not str(record["original"].get("oid", "")).isdigit()
            ):
                raise ServicePostgresError(
                    "The retained PostgreSQL database binding is invalid."
                )
            names.add(name)
        return value

    def _positions(self, connection, record):
        positions = {
            name: self._database(connection, record[name])
            for name in ("target", "scratch", "retained")
        }
        old_oid, new_oid = record["original"]["oid"], record["new_oid"]
        for location, database in positions.items():
            if database is None:
                continue
            if database["oid"] == old_oid:
                if (
                    location not in {"target", "retained"}
                    or database["owner_oid"] != record["original"]["owner_oid"]
                    or database["comment"]
                    not in {record["old_tag"], record["original"]["comment"]}
                ):
                    raise ServicePostgresError(
                        "The retained original PostgreSQL database identity changed."
                    )
            elif database["oid"] == new_oid:
                comments = [record["new_tag"]]
                if record.get("release_intent") is True:
                    comments.append(record["settings"].get("comment"))
                if (
                    location not in {"target", "scratch"}
                    or database["comment"] not in comments
                ):
                    raise ServicePostgresError(
                        "The restore-owned PostgreSQL database identity changed."
                    )
            else:
                raise ServicePostgresError(
                    "An unrelated PostgreSQL database occupies a restore transaction name."
                )
        return positions

    def apply_restore(self, transaction, persist):
        transaction = self._transaction(transaction)
        self.verify_server_binding(transaction)
        if transaction["phase"] not in {"prepared", "applying", "applied"} or any(
            record.get("ready") is not True for record in transaction["databases"]
        ):
            raise ServicePostgresError(
                "Only a completely verified database restore can be activated."
            )
        with self._locked(transaction["cluster"]) as (connection, _cluster):
            transaction["phase"] = "applying"
            persist(transaction)
            for record in transaction["databases"]:
                positions = self._positions(connection, record)
                if (
                    positions["target"]
                    and positions["target"]["oid"] == record["new_oid"]
                    and positions["retained"]
                ):
                    continue
                if (
                    not positions["target"]
                    or not positions["scratch"]
                    or positions["retained"] is not None
                ):
                    raise ServicePostgresError(
                        "The selected database transaction cannot be safely activated."
                    )
                self._execute(
                    connection,
                    sql.SQL("COMMENT ON DATABASE {} IS %s").format(
                        sql.Identifier(record["target"])
                    ),
                    (record["old_tag"],),
                )
                for field in ("target", "scratch"):
                    self._execute(
                        connection,
                        sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(
                            sql.Identifier(record[field])
                        ),
                    )
                self._require_idle(connection, record["original"]["oid"])
                self._require_idle(connection, record["new_oid"])
            connection.autocommit = False
            try:
                # Renames for every selected logical database commit together.
                # A lost journal acknowledgement is recovered from pinned OIDs.
                for record in transaction["databases"]:
                    positions = self._positions(connection, record)
                    if (
                        positions["target"]
                        and positions["target"]["oid"] == record["new_oid"]
                    ):
                        continue
                    self._execute(
                        connection,
                        sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                            sql.Identifier(record["target"]),
                            sql.Identifier(record["retained"]),
                        ),
                    )
                    self._execute(
                        connection,
                        sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                            sql.Identifier(record["scratch"]),
                            sql.Identifier(record["target"]),
                        ),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.autocommit = True
            transaction["phase"] = "applied"
            persist(transaction)

    def rollback_restore(self, transaction, persist):
        transaction = self._transaction(transaction)
        self.verify_server_binding(transaction)
        if transaction["phase"] in {"finishing", "finished"}:
            raise ServicePostgresError(
                "A committed database restore cannot be rolled back after recovery cleanup starts."
            )
        with self._locked(transaction["cluster"]) as (connection, _cluster):
            transaction["phase"] = "rolling_back"
            persist(transaction)
            for record in reversed(transaction["databases"]):
                positions = self._positions(connection, record)
                current = positions["target"]
                if current is not None and current["oid"] == record["new_oid"]:
                    if (
                        positions["retained"] is None
                        or positions["scratch"] is not None
                    ):
                        raise ServicePostgresError(
                            "The original PostgreSQL recovery database is unavailable."
                        )
                    self._execute(
                        connection,
                        sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(
                            sql.Identifier(record["target"])
                        ),
                    )
                    self._require_idle(connection, record["new_oid"])
                    connection.autocommit = False
                    try:
                        self._execute(
                            connection,
                            sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                                sql.Identifier(record["target"]),
                                sql.Identifier(record["scratch"]),
                            ),
                        )
                        self._execute(
                            connection,
                            sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                                sql.Identifier(record["retained"]),
                                sql.Identifier(record["target"]),
                            ),
                        )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    finally:
                        connection.autocommit = True
                original = self._database(connection, record["target"])
                if original is None or original["oid"] != record["original"]["oid"]:
                    raise ServicePostgresError(
                        "The original PostgreSQL database identity is unavailable."
                    )
                self._execute(
                    connection,
                    sql.SQL("COMMENT ON DATABASE {} IS %s").format(
                        sql.Identifier(record["target"])
                    ),
                    (record["original"]["comment"],),
                )
                self._execute(
                    connection,
                    sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS {}").format(
                        sql.Identifier(record["target"]),
                        sql.Literal(record["original"]["allow_connections"]),
                    ),
                )
                self._drop_database(
                    connection, record, "scratch", record["new_oid"], record["new_tag"]
                )
                persist(transaction)
            self._drop_role(connection, transaction["role"])
            transaction["phase"] = "rolled_back"
            persist(transaction)

    def _drop_database(self, connection, record, field, oid, tag):
        database = self._database(connection, record[field])
        if database is None:
            return
        if oid is None or database["oid"] != oid or database["comment"] != tag:
            raise ServicePostgresError(
                "A restore database cannot be removed because its ownership proof changed."
            )
        self._execute(
            connection,
            sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(
                sql.Identifier(record[field])
            ),
        )
        self._require_idle(connection, oid)
        self._execute(
            connection,
            sql.SQL("DROP DATABASE {}").format(sql.Identifier(record[field])),
        )

    def finish_restore(self, transaction, persist):
        """Commit cleanup only after the caller's file/config transaction commits.

        The replacement databases stay connection-blocked until old databases
        are removed. This method is idempotent after a process restart.
        """
        transaction = self._transaction(transaction)
        self.verify_server_binding(transaction)
        if transaction["phase"] not in {"applied", "finishing", "finished"}:
            raise ServicePostgresError("The database restore is not ready to commit.")
        with self._locked(transaction["cluster"]) as (connection, _cluster):
            transaction["phase"] = "finishing"
            persist(transaction)
            for record in transaction["databases"]:
                positions = self._positions(connection, record)
                current = positions["target"]
                if (
                    current is None
                    or current["oid"] != record["new_oid"]
                    or positions["scratch"] is not None
                ):
                    raise ServicePostgresError(
                        "The activated PostgreSQL database identity changed."
                    )
                self._drop_database(
                    connection,
                    record,
                    "retained",
                    record["original"]["oid"],
                    record["old_tag"],
                )
                # Restore the archived database's ordinary comment and owner
                # permissions only after its recovery copy has been retired.
                record["release_intent"] = True
                persist(transaction)
                self._execute(
                    connection,
                    sql.SQL("COMMENT ON DATABASE {} IS %s").format(
                        sql.Identifier(record["target"])
                    ),
                    (record["settings"].get("comment"),),
                )
                self._execute(
                    connection,
                    sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO PUBLIC").format(
                        sql.Identifier(record["target"])
                    ),
                )
                self._execute(
                    connection,
                    sql.SQL(
                        "ALTER DATABASE {} WITH ALLOW_CONNECTIONS {} CONNECTION LIMIT {}"
                    ).format(
                        sql.Identifier(record["target"]),
                        sql.Literal(record["settings"]["allow_connections"]),
                        sql.Literal(record["settings"]["connection_limit"]),
                    ),
                )
                persist(transaction)
            transaction["phase"] = "finished"
            persist(transaction)
