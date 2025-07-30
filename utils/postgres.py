from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
import psycopg2, time, os, subprocess, json, glob
from psycopg2 import sql
from time import sleep
import shutil, tempfile


config = CONFIG_MANAGER
user_id = config.get("puid")
group_id = config.get("pgid")


def initialize_postgres_databases(
    postgres_host, postgres_port, postgres_user, postgres_password, postgres_databases
):
    try:
        retries = 5
        while retries > 0:
            try:
                conn = psycopg2.connect(
                    dbname="postgres",
                    user=postgres_user,
                    password=postgres_password,
                    host=postgres_host,
                    port=postgres_port,
                )
                break
            except psycopg2.OperationalError as e:
                if "is not yet accepting connections" in str(e).lower():
                    logger.info(
                        "Database system is still recovering. Retrying initialization..."
                    )
                    time.sleep(5)
                    retries -= 1
                else:
                    raise e
        else:
            return False, "Database system did not recover within the retry period."

        conn.autocommit = True
        cur = conn.cursor()

        for db_config in postgres_databases:
            db_name = db_config["name"]

            if not db_config.get("enabled", True):
                logger.info(f"Skipping initialization for database '{db_name}'.")
                continue

            logger.info(f"Checking if database '{db_name}' exists...")
            cur.execute(
                sql.SQL("SELECT 1 FROM pg_database WHERE datname = %s"), [db_name]
            )
            if cur.fetchone() is None:
                cur.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(db_name), sql.Identifier(postgres_user)
                    )
                )
                logger.info(
                    f"Database '{db_name}' created successfully with owner '{postgres_user}'."
                )
            else:
                logger.info(f"Database '{db_name}' already exists.")

            logger.info(f"Enabling system_stats extension on '{db_name}' database...")
            cur.execute(
                sql.SQL("SELECT 1 FROM pg_extension WHERE extname = 'system_stats';")
            )
            if cur.fetchone() is None:
                cur.execute(sql.SQL("CREATE EXTENSION system_stats;"))
                logger.info("system_stats extension enabled successfully.")
            else:
                logger.info("system_stats extension is already enabled.")

            logger.info(
                f"Checking if pgAgent extension is enabled on '{db_name}' database..."
            )
            cur.execute(
                sql.SQL("SELECT 1 FROM pg_extension WHERE extname = 'pgagent';")
            )
            if cur.fetchone() is None:
                cur.execute(sql.SQL("CREATE EXTENSION pgagent;"))
                logger.info("pgAgent extension enabled successfully.")
            else:
                logger.info("pgAgent extension is already enabled.")

        cur.close()
        conn.close()
        return True, None
    except Exception as e:
        return False, f"Error initializing PostgreSQL databases: {e}"


def create_default_postgresql_conf(postgres_config_dir, postgres_config_file):
    try:
        with open(postgres_config_file, "w") as config_file:
            config_file.write("# PostgreSQL configuration file\n")
            config_file.write("listen_addresses = '*'\n")
            config_file.write("port = 5432\n")
            config_file.write("max_connections = 100\n")
            config_file.write("shared_buffers = 128MB\n")
        logger.info(
            f"Created default PostgreSQL configuration file at {postgres_config_file}."
        )
        return True, None
    except OSError as e:
        return False, f"Error creating PostgreSQL configuration file: {e}"


def ensure_run_directory():
    try:
        run_dir = "/run/postgresql"
        if not os.path.exists(run_dir):
            os.makedirs(run_dir)
            logger.debug(f"Created directory {run_dir}.")
        subprocess.run(["chown", "-R", f"{user_id}:{group_id}", run_dir], check=True)
        subprocess.run(["chmod", "775", run_dir], check=True)
        logger.debug(f"Set ownership and permissions for {run_dir}.")
        return True, None
    except Exception as e:
        return False, f"Error setting up /run/postgresql directory: {e}"


def initialize_postgres_config_dir_directory(
    process_handler, postgres_config_dir, postgres_user, postgres_password
):
    try:
        postmaster_opts = os.path.join(postgres_config_dir, "postmaster.opts")
        if os.path.exists(postmaster_opts):
            logger.debug(
                f"PostgreSQL data directory {postgres_config_dir} already exists and is not empty. Skipping initialization."
            )
            return True, None
        else:
            logger.info(
                f"Initializing PostgreSQL data directory at {postgres_config_dir}..."
            )
            initialize_command = f"initdb -D {postgres_config_dir} -U {postgres_user} --pwfile=<(echo {postgres_password})"
            init = process_handler.start_process(
                "PostgreSQL_init",
                postgres_config_dir,
                ["su", "-", postgres_user, "-s", "/bin/bash", "-c", initialize_command],
            )
            process_handler.wait("PostgreSQL_init")
            logger.info(
                f"Initialized PostgreSQL data directory at {postgres_config_dir}."
            )
            return True, None
    except subprocess.CalledProcessError as e:
        return False, f"Error initializing PostgreSQL data directory: {e}"


def check_postgresql_started(
    postgres_host, postgres_port, postgres_user, postgres_databases, timeout=60
):
    enabled_databases = [
        db["name"] for db in postgres_databases if db.get("enabled", True)
    ]

    if not enabled_databases:
        logger.info("No databases are enabled for readiness checks.")
        return True, None

    logger.info("Checking if PostgreSQL server has started...")
    logger.debug(f"Postgres Port: {postgres_port}")
    start_time = time.time()

    while time.time() - start_time < timeout:
        all_ready = True
        for db in enabled_databases:
            result = subprocess.run(
                [
                    "pg_isready",
                    "-U",
                    postgres_user,
                    "-d",
                    db,
                    "-h",
                    postgres_host,
                    "-p",
                    str(postgres_port),
                ],
                capture_output=True,
                text=True,
            )
            if "recovery" in result.stderr.lower():
                logger.info(f"Database '{db}' is still in recovery mode. Retrying...")
                all_ready = False
            elif result.returncode != 0:
                logger.debug(
                    f"PostgreSQL database '{db}' not ready: {result.stdout.strip()} {result.stderr.strip()}"
                )
                all_ready = False
            else:
                logger.info(f"PostgreSQL database '{db}' is ready.")

        if all_ready:
            logger.info("All enabled PostgreSQL databases are ready.")
            return True, None

        time.sleep(2)

    return False, "PostgreSQL server failed to start within the timeout period."


def check_postgresql_ready(
    postgres_host,
    postgres_port,
    postgres_user,
    postgres_password,
    postgres_databases,
    timeout=60,
):
    enabled_databases = [
        db["name"] for db in postgres_databases if db.get("enabled", True)
    ]

    if not enabled_databases:
        logger.info("No databases are enabled for health checks.")
        return True, None

    logger.info("Checking if PostgreSQL databases are healthy and accessible...")
    start_time = time.time()

    while time.time() - start_time < timeout:
        all_healthy = True
        for db in enabled_databases:
            try:
                conn = psycopg2.connect(
                    dbname=db,
                    user=postgres_user,
                    password=postgres_password,
                    host=postgres_host,
                    port=postgres_port,
                )
                cur = conn.cursor()
                cur.execute("SELECT pg_is_in_recovery();")
                in_recovery = cur.fetchone()[0]
                if in_recovery:
                    logger.debug(
                        f"PostgreSQL database '{db}' is still in recovery mode. Waiting..."
                    )
                    all_healthy = False
                else:
                    logger.info(f"PostgreSQL database '{db}' is healthy and ready.")
                cur.close()
                conn.close()
            except psycopg2.OperationalError as e:
                logger.debug(f"Database '{db}' not ready yet: {e}")
                all_healthy = False
            except Exception as e:
                logger.error(f"Unexpected error while checking database '{db}': {e}")
                all_healthy = False

        if all_healthy:
            logger.info("All enabled PostgreSQL databases are healthy and ready.")
            return True, None

        time.sleep(2)

    return (
        False,
        "PostgreSQL databases did not become healthy within the timeout period.",
    )


def initialize_pgadmin_config_directory(pgadmin_config_dir):
    try:
        if not os.path.exists(pgadmin_config_dir):
            os.makedirs(pgadmin_config_dir, exist_ok=True)
            logger.info(f"Created pgAdmin data directory at {pgadmin_config_dir}.")
        else:
            logger.info(f"pgAdmin data directory exists at {pgadmin_config_dir}.")
        subprocess.run(
            ["chown", "-R", f"{user_id}:{group_id}", pgadmin_config_dir], check=True
        )
        subprocess.run(["chmod", "-R", "700", pgadmin_config_dir], check=True)
        logger.info(f"Changed ownership and set permissions of {pgadmin_config_dir}.")
        return True, None
    except OSError as e:
        return False, f"Error creating pgAdmin data directory: {e}"


def create_pgadmin_config(
    pgadmin_config_dir,
    pgadmin_db_uri,
    pgadmin_config_file,
    pgadmin_default_server,
    pgadmin_port,
):
    pgadmin_install_dirs = glob.glob(
        "/pgadmin/venv/lib/python*/site-packages/pgadmin4/"
    )

    if not pgadmin_install_dirs:
        return False, "pgAdmin installation directory not found."

    pgadmin_install_dir = pgadmin_install_dirs[0]
    symlink_path = os.path.join(pgadmin_install_dir, "config_local.py")

    try:
        with open(pgadmin_config_file, "w") as config_file:
            config_file.write("SERVER_MODE = True\n")
            config_file.write(f"DATA_DIR = '{pgadmin_config_dir}'\n")
            config_file.write(f"DEFAULT_SERVER = '{pgadmin_default_server}'\n")
            config_file.write(f"DEFAULT_SERVER_PORT = {int(pgadmin_port)}\n")
            config_file.write(f"LOG_FILE = '{pgadmin_config_dir}/pgadmin4.log'\n")
            config_file.write(f"CONFIG_DATABASE_URI = '{pgadmin_db_uri}'\n")
            config_file.write(f"SESSION_DB_PATH = '{pgadmin_config_dir}/sessions'\n")
            config_file.write(f"STORAGE_DIR = '{pgadmin_config_dir}/storage'\n")
            config_file.write("DEFAULT_BINARY_PATHS = {\n")
            config_file.write("    'pg': '/usr/bin',\n")
            config_file.write("    'pg-12': '/usr/bin',\n")
            config_file.write("    'pg-13': '/usr/bin',\n")
            config_file.write("    'pg-14': '/usr/bin',\n")
            config_file.write("    'pg-15': '/usr/bin',\n")
            config_file.write("    'pg-16': '/usr/bin',\n")
            config_file.write("    'pg-17': '/usr/bin',\n")
            config_file.write("    'ppas': '/usr/bin',\n")
            config_file.write("    'ppas-12': '/usr/bin',\n")
            config_file.write("    'ppas-13': '/usr/bin',\n")
            config_file.write("    'ppas-14': '/usr/bin',\n")
            config_file.write("    'ppas-15': '/usr/bin',\n")
            config_file.write("    'ppas-16': '/usr/bin',\n")
            config_file.write("    'ppas-17': '/usr/bin'\n")
            config_file.write("}\n")

        subprocess.run(
            ["chown", f"{user_id}:{group_id}", pgadmin_config_file], check=True
        )
        logger.info(f"Created pgAdmin configuration file at {pgadmin_config_file}.")
    except OSError as e:
        return False, f"Error creating pgAdmin configuration file: {e}"
    try:
        if os.path.exists(symlink_path):
            os.remove(symlink_path)
        os.symlink(pgadmin_config_file, symlink_path)
        logger.info(f"Created symlink for config_local.py at {symlink_path}.")
    except OSError as e:
        return False, f"Error creating symlink for pgAdmin configuration file: {e}"

    return pgadmin_config_file


def start_pgadmin(
    process_handler,
    pgadmin_config_dir,
    pgadmin_config_file,
    pgadmin_db_uri,
    pgadmin_email,
    pgadmin_password,
    pgadmin_process_name,
    pgadmin_port,
    pgadmin_default_server,
):
    logger.info("Starting pgAdmin process with PostgreSQL database...")
    pgadmin_venv = "/pgadmin/venv"

    try:
        config_file = create_pgadmin_config(
            pgadmin_config_dir,
            pgadmin_db_uri,
            pgadmin_config_file,
            pgadmin_default_server,
            pgadmin_port,
        )
        if not config_file:
            return False
        os.environ["PGADMIN_SETUP_EMAIL"] = pgadmin_email
        os.environ["PGADMIN_SETUP_PASSWORD"] = pgadmin_password
        env = os.environ.copy()
        env["PATH"] = f"{pgadmin_venv}/bin:" + env["PATH"]
        env["VIRTUAL_ENV"] = pgadmin_venv

        pgadmin_command = f"{pgadmin_venv}/bin/python3 /pgadmin/venv/lib/python*/site-packages/pgadmin4/pgAdmin4.py"
        # set the command in the config memory
        config.set("pgadmin", "command", pgadmin_command)

        process_handler.start_process(
            pgadmin_process_name,
            pgadmin_config_dir,
            ["sh", "-c", f"exec {pgadmin_command}"],
            env=env,
        )

        logger.info("pgAdmin process started successfully using PostgreSQL database.")
        return True, None
    except Exception as e:
        return False, f"Error starting pgAdmin: {e}"


def postgres_role_exists(
    postgres_user, postgres_password, postgres_databases, postgres_host, postgres_port
):
    logger.info(f"Checking if role '{postgres_user}' exists...")
    try:
        conn = psycopg2.connect(
            dbname=postgres_databases,
            user=postgres_user,
            password=postgres_password,
            host=postgres_host,
            port=postgres_port,
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s;", (postgres_user,))
        role_exists = cur.fetchone() is not None
        cur.close()
        conn.close()
        return role_exists, None
    except Exception as e:
        return False, f"Error checking if role '{postgres_user}' exists: {e}"


def list_database_sizes(postgres_host, postgres_port, postgres_user, postgres_password):
    logger.info("Listing PostgreSQL database sizes...")
    try:
        conn = psycopg2.connect(
            dbname="postgres",
            user=postgres_user,
            password=postgres_password,
            host=postgres_host,
            port=postgres_port,
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_database.datname, pg_size_pretty(pg_database_size(pg_database.datname)) AS size FROM pg_database;"
        )
        rows = cur.fetchall()
        for row in rows:
            logger.info(f"Database: {row[0]}, Size: {row[1]}")
        cur.close()
        conn.close()
        return True, None
    except Exception as e:
        return False, f"Error listing database sizes: {e}"


def start_pgagent(process_handler, postgres_user, postgres_host, postgres_port):
    try:
        pgagent_command = f'pgagent -f "host={postgres_host} port={postgres_port} dbname=postgres user={postgres_user}"'
        success, error = process_handler.start_process(
            "pgAgent",
            "/usr/bin/",
            ["/bin/bash", "-c", pgagent_command],
        )
        if not success:
            return False, error
        return True, None
    except Exception as e:
        return False, f"Error starting pgAgent daemon: {e}"


def add_pgadmin_server_to_db(pgadmin_db_uri, server_details, timeout=60, interval=5):
    try:
        sleep(5)
        logger.info(
            f"Connecting to pgAdmin database at {pgadmin_db_uri} to add server '{server_details['name']}'..."
        )
        conn = psycopg2.connect(pgadmin_db_uri)
        conn.autocommit = True
        cur = conn.cursor()
        start_time = time.time()
        while time.time() - start_time < timeout:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 
                    FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND table_name = 'server'
                );
            """
            )
            table_exists = cur.fetchone()[0]

            if table_exists:
                logger.info(
                    "pgAdmin 'server' table exists. Proceeding to check server."
                )
                break
            else:
                logger.info(
                    f"pgAdmin 'server' table does not exist yet. Waiting for {interval} seconds before retrying..."
                )
                time.sleep(interval)
        else:
            cur.close()
            conn.close()
            return (
                False,
                f"Timeout reached after {timeout} seconds. The pgAdmin 'server' table still does not exist.",
            )

        try:
            server_port = int(server_details["port"])
        except ValueError:
            return False, f"Invalid port value: {server_details['port']}"
        server_name = server_details["name"].strip()
        server_host = server_details["host"].strip()
        logger.debug(
            f"Checking if server '{server_name}' with host '{server_host}' and port '{server_port}' already exists in the database."
        )
        cur.execute(
            """
            SELECT name, host, port FROM server WHERE name = %s AND host = %s AND port = %s
        """,
            (server_name, server_host, server_port),
        )
        server_exists = cur.fetchone()
        logger.debug(f"Query result: {server_exists}")
        if server_exists:
            logger.info(f"Server '{server_name}' already exists in pgAdmin database.")
            cur.close()
            conn.close()
            return True, None

        connection_params_json = json.dumps(server_details["connection_parameters"])
        cur.execute(
            """
            INSERT INTO server (user_id, servergroup_id, name, host, port, maintenance_db, username, connection_params)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
            (
                1,
                1,
                server_name,
                server_host,
                server_port,
                server_details["maintenance_db"].strip(),
                server_details["username"].strip(),
                connection_params_json,
            ),
        )
        conn.commit()
        logger.info(
            f"Server '{server_name}' added successfully to pgAdmin database. The password will need to be entered in pgAdmin 4 upon first connection."
        )
        cur.close()
        conn.close()
        return True, None

    except Exception as e:
        return False, f"Error adding server to pgAdmin database: {e}"


def update_postgresql_conf(config_file, postgres_config):
    try:
        with open(config_file, "r") as f:
            lines = f.readlines()

        updated_lines = []

        for line in lines:
            stripped_line = line.strip()
            updated = False

            for config_key, config_value in postgres_config.items():
                if stripped_line.startswith(
                    f"{config_key} "
                ) or stripped_line.startswith(f"#{config_key} "):
                    if config_value:
                        updated_lines.append(f"{config_key} = {config_value}\n")
                    else:
                        updated_lines.append(f"#{config_key} = \n")
                    updated = True
                    break

            if not updated:
                updated_lines.append(line)

        with open(config_file, "w") as f:
            f.writelines(updated_lines)

        logger.info(f"Updated PostgreSQL configuration file at {config_file}.")
        return True, None

    except Exception as e:
        return False, f"Error updating PostgreSQL configuration file: {e}"


def pgadmin_setup(process_handler):
    pgadmin_config = config.get("pgadmin")
    postgres_config = config.get("postgres")
    postgres_process_name = postgres_config.get("process_name")
    postgres_host = postgres_config.get("host")
    postgres_user = postgres_config.get("user")
    postgres_password = postgres_config.get("password")
    postgres_port = postgres_config.get("port")
    pgadmin_config_dir = pgadmin_config.get("config_dir")
    pgadmin_config_file = pgadmin_config.get("config_file")
    pgadmin_process_name = pgadmin_config.get("process_name")
    pgadmin_email = pgadmin_config.get("setup_email")
    pgadmin_password = pgadmin_config.get("setup_password")
    pgadmin_port = pgadmin_config.get("port")
    pgadmin_default_server = pgadmin_config.get("default_server")
    pgadmin_email = pgadmin_config.get("setup_email")
    pgadmin_password = pgadmin_config.get("setup_password")
    try:
        setup_email = pgadmin_config.get("setup_email", "").strip()
        setup_password = pgadmin_config.get("setup_password", "").strip()

        if not setup_email or not setup_password:
            logger.warning(
                "PGADMIN_SETUP_EMAIL or PGADMIN_SETUP_PASSWORD not set. Skipping pgAdmin setup."
            )
            return True, None

        if postgres_process_name not in process_handler.process_names:
            return (
                False,
                f"PostgreSQL process '{postgres_process_name}' is not running. Cannot proceed with pgAdmin setup.",
            )

        success, error = initialize_pgadmin_config_directory(pgadmin_config_dir)
        if not success:
            return False, error

        pgadmin_db_uri = f"postgresql://{postgres_user}:{postgres_password}@{postgres_host}:{postgres_port}/pgadmin"

        process_handler.setup_tracker.add(pgadmin_process_name)
        success, error = start_pgadmin(
            process_handler,
            pgadmin_config_dir,
            pgadmin_config_file,
            pgadmin_db_uri,
            pgadmin_email,
            pgadmin_password,
            pgadmin_process_name,
            pgadmin_port,
            pgadmin_default_server,
        )
        if not success:
            process_handler.setup_tracker.remove(pgadmin_process_name)
            return False, error

        server_details = {
            "name": postgres_user,
            "host": postgres_host,
            "port": postgres_port,
            "maintenance_db": "postgres",
            "username": postgres_user,
            "connection_parameters": {"connect_timeout": 10},
        }

        success, error = add_pgadmin_server_to_db(pgadmin_db_uri, server_details)
        if not success:
            process_handler.setup_tracker.remove(pgadmin_process_name)
            return False, error

        success, error = start_pgagent(
            process_handler, postgres_user, postgres_host, postgres_port
        )
        if not success:
            return False, error

        logger.info("pgAdmin setup completed successfully.")
        process_handler.setup_tracker.add(pgadmin_process_name)
        return True, None

    except Exception as e:
        process_handler.setup_tracker.remove(pgadmin_process_name)
        return False, f"Unhandled exception during pgAdmin setup: {e}"


def migrate_legacy_role_dmb_to_dumb(
    postgres_host: str,
    postgres_port: int,
    postgres_password: str,
    process_handler,
    postgres_process_name: str,
    postgres_config_dir: str,
    postgres_command: str,
):
    try:
        role_exists, error = postgres_role_exists(
            "DUMB", postgres_password, "postgres", postgres_host, postgres_port
        )
        if role_exists:
            logger.info("Role 'DUMB' already exists. No migration required.")
            return True, None
        elif error:
            logger.info("Ignore the CRITICAL message; proceeding with migration.")

        try:
            conn = psycopg2.connect(
                dbname="postgres",
                user="DMB",
                password=postgres_password,
                host=postgres_host,
                port=postgres_port,
            )
            conn.close()
        except psycopg2.OperationalError:
            logger.info(
                "Role 'DMB' does not exist or cannot connect. No migration needed."
            )
            return True, None

        logger.info(
            "Connected as legacy role 'DMB'. Starting pg_dumpall migration to 'DUMB'..."
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            roles_path = os.path.join(tmpdir, "roles.sql")
            dbnames = []
            with psycopg2.connect(
                dbname="postgres",
                user="DMB",
                password=postgres_password,
                host=postgres_host,
                port=postgres_port,
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT datname FROM pg_database WHERE datdba = (SELECT oid FROM pg_roles WHERE rolname = 'DMB') AND datistemplate = false;"
                    )
                    dbnames = [row[0] for row in cur.fetchall()]
                    dbnames = [db for db in dbnames if db != "postgres"]

            logger.info(f"Found {len(dbnames)} databases owned by 'DMB': {dbnames}")
            logger.info(
                "Checking database sizes before migration. This may take a moment..."
            )

            success, error = list_database_sizes(
                postgres_host, postgres_port, "DMB", postgres_password
            )
            if not success:
                logger.warning(error)

            db_paths = {}
            logger.info("Dumping roles using pg_dumpall...")
            dump_roles_cmd = [
                "pg_dumpall",
                f"-h{postgres_host}",
                f"-p{postgres_port}",
                "-U",
                "DMB",
                "--roles-only",
                "-f",
                roles_path,
            ]
            env = os.environ.copy()
            env["PGPASSWORD"] = postgres_password

            result = subprocess.run(
                dump_roles_cmd, env=env, capture_output=True, text=True
            )
            if result.returncode != 0:
                logger.error(f"pg_dumpall (roles-only) failed: {result.stderr}")
                return False, result.stderr

            for dbname in dbnames:
                db_path = os.path.join(tmpdir, f"{dbname}.sql")
                db_paths[dbname] = db_path
                logger.info(f"Dumping database '{dbname}'...")
                dump_db_cmd = [
                    "pg_dump",
                    "-C",
                    f"-h{postgres_host}",
                    f"-p{postgres_port}",
                    "-U",
                    "DMB",
                    "-d",
                    dbname,
                    "-f",
                    db_path,
                ]
                result = subprocess.run(
                    dump_db_cmd, env=env, capture_output=True, text=True
                )
                if result.returncode != 0:
                    logger.error(f"pg_dump for '{dbname}' failed: {result.stderr}")
                    return False, result.stderr

            logger.info(
                "PostgreSQL dump complete. Proceeding with cluster reinitialization..."
            )

            process_handler.stop_process(postgres_process_name)
            process_handler.wait(postgres_process_name)

            backup_dir = f"{postgres_config_dir}_backup"
            os.makedirs(backup_dir, exist_ok=True)
            for item in os.listdir(postgres_config_dir):
                shutil.move(
                    os.path.join(postgres_config_dir, item),
                    os.path.join(backup_dir, item),
                )

            success, error = initialize_postgres_config_dir_directory(
                process_handler=process_handler,
                postgres_config_dir=postgres_config_dir,
                postgres_user="DUMB",
                postgres_password=postgres_password,
            )
            if not success:
                return (
                    False,
                    f"Failed to initialize new PostgreSQL config directory: {error}",
                )

            logger.info("New PostgreSQL config directory initialized.")

            process = process_handler.start_process(
                postgres_process_name, postgres_config_dir, postgres_command
            )
            if not process:
                return (
                    False,
                    "Failed to restart PostgreSQL process with new configuration.",
                )

            logger.info("Waiting for PostgreSQL to become ready...")
            success, error = check_postgresql_started(
                postgres_host,
                postgres_port,
                postgres_user="postgres",
                postgres_databases=[{"name": "postgres", "enabled": True}],
            )
            if not success:
                return False, f"PostgreSQL did not become ready: {error}"

            sleep(5)

            logger.info("Restoring roles using psql as 'DUMB'...")
            roles_restore_cmd = [
                "psql",
                f"-h{postgres_host}",
                f"-p{postgres_port}",
                "-U",
                "DUMB",
                "-d",
                "postgres",
                "-f",
                roles_path,
            ]
            result = subprocess.run(
                roles_restore_cmd, env=env, capture_output=True, text=True
            )
            if result.returncode != 0:
                logger.error(f"Roles restore failed: {result.stderr}")
                return False, result.stderr

            for dbname, path in db_paths.items():
                logger.info(f"Restoring database '{dbname}' using psql as 'DUMB'...")
                db_restore_cmd = [
                    "psql",
                    f"-h{postgres_host}",
                    f"-p{postgres_port}",
                    "-U",
                    "DUMB",
                    "-d",
                    "postgres",
                    "-f",
                    path,
                ]
                result = subprocess.run(
                    db_restore_cmd, env=env, capture_output=True, text=True
                )
                if result.returncode != 0:
                    logger.error(f"Restore of {dbname} failed: {result.stderr}")
                    return False, result.stderr

            for dbname in db_paths.keys():
                logger.info(
                    f"Reassigning objects in '{dbname}' from 'DMB' to 'DUMB'..."
                )
                conn = psycopg2.connect(
                    dbname=dbname,
                    user="DUMB",
                    password=postgres_password,
                    host=postgres_host,
                    port=postgres_port,
                )
                conn.autocommit = True
                cur = conn.cursor()
                cur.execute('REASSIGN OWNED BY "DMB" TO "DUMB";')
                cur.execute('DROP OWNED BY "DMB";')
                cur.close()
                conn.close()

            conn = psycopg2.connect(
                dbname="postgres",
                user="DUMB",
                password=postgres_password,
                host=postgres_host,
                port=postgres_port,
            )
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute('DROP ROLE IF EXISTS "DMB";')
            cur.close()
            conn.close()
            logger.info("Successfully dropped role 'DMB'.")

        for dbname in db_paths.keys():
            logger.info(f"Ensuring useful extensions exist in database '{dbname}'...")
            try:
                conn = psycopg2.connect(
                    dbname=dbname,
                    user="DUMB",
                    password=postgres_password,
                    host=postgres_host,
                    port=postgres_port,
                )
                conn.autocommit = True
                cur = conn.cursor()

                extensions = ["pgagent", "system_stats"]
                for ext in extensions:
                    cur.execute(f"CREATE EXTENSION IF NOT EXISTS {ext};")
                    logger.info(f"Extension '{ext}' ensured in '{dbname}'.")

                cur.close()
                conn.close()
            except Exception as e:
                logger.warning(f"Failed to create extensions in '{dbname}': {e}")

        logger.info("Migration from DMB to DUMB complete.")
        return True, None

    except Exception as e:
        logger.warning(f"Migration logic failed: {e}")
        return False, str(e)


def postgres_setup(process_handler=None):
    postgres_config = config.get("postgres")
    postgres_config_dir = postgres_config.get("config_dir")
    postgres_config_file = postgres_config.get("config_file")
    postgres_process_name = postgres_config.get("process_name")
    postgres_host = postgres_config.get("host")
    postgres_user = postgres_config.get("user")
    postgres_password = postgres_config.get("password")
    postgres_port = postgres_config.get("port")
    postgres_databases = postgres_config.get("databases")
    try:
        if postgres_config_dir:
            if not os.path.exists(postgres_config_dir):
                try:
                    os.makedirs(postgres_config_dir, exist_ok=True)
                    logger.info(
                        f"Created PostgreSQL data directory at {postgres_config_dir}."
                    )
                except OSError as e:
                    return False, f"Error creating PostgreSQL data directory: {e}"
            else:
                logger.info(
                    f"PostgreSQL data directory exists at {postgres_config_dir}."
                )

        try:
            subprocess.run(
                ["chown", "-R", f"{user_id}:{group_id}", postgres_config_dir],
                check=True,
            )
            subprocess.run(["chmod", "-R", "700", postgres_config_dir], check=True)
            logger.info(
                f"Changed ownership and set permissions of {postgres_config_dir}."
            )
        except subprocess.CalledProcessError as e:
            return False, f"Error setting permissions for {postgres_config_dir}: {e}"

        if os.path.exists(os.path.join(postgres_config_dir, "postmaster.pid")):
            os.remove(os.path.join(postgres_config_dir, "postmaster.pid"))

        success, error = initialize_postgres_config_dir_directory(
            process_handler, postgres_config_dir, postgres_user, postgres_password
        )
        if not success:
            return False, error

        success, error = ensure_run_directory()
        if not success:
            return False, error

        if not os.path.exists(postgres_config_file):
            logger.warning(
                "PostgreSQL configuration file not found. Creating default configuration."
            )
            success, error = create_default_postgresql_conf(
                postgres_config_dir, postgres_config_file
            )
            if not success:
                return False, error
        else:
            success, error = update_postgresql_conf(
                postgres_config_file, postgres_config
            )
            if not success:
                return False, error

        postgres_command_template = postgres_config.get("command")
        if isinstance(postgres_command_template, list):
            postgres_command_template = " ".join(postgres_command_template)

        postgres_command = postgres_command_template.format(
            postgres_config_dir=postgres_config_dir,
            postgres_config_file=postgres_config_file,
        )
        process_handler.setup_tracker.add(postgres_process_name)
        process = process_handler.start_process(
            postgres_process_name, postgres_config_dir, postgres_command
        )
        if not process:
            return False, "Failed to start PostgreSQL process."

        success, error = check_postgresql_started(
            postgres_host,
            postgres_port,
            postgres_user,
            postgres_databases=[{"name": "postgres", "enabled": True}],
        )
        if not success:
            return False, error

        sleep(10)

        success, error = migrate_legacy_role_dmb_to_dumb(
            postgres_host=postgres_host,
            postgres_port=postgres_port,
            postgres_password=postgres_password,
            process_handler=process_handler,
            postgres_process_name=postgres_process_name,
            postgres_config_dir=postgres_config_dir,
            postgres_command=postgres_command,
        )
        if not success:
            logger.warning(error)

        success, error = initialize_postgres_databases(
            postgres_host,
            postgres_port,
            postgres_user,
            postgres_password,
            postgres_databases,
        )
        if not success:
            return False, error

        success, error = check_postgresql_ready(
            postgres_host,
            postgres_port,
            postgres_user,
            postgres_password,
            postgres_databases,
        )
        if not success:
            return False, error

        success, error = list_database_sizes(
            postgres_host, postgres_port, postgres_user, postgres_password
        )
        if not success:
            logger.warning(error)

        logger.info("PostgreSQL setup completed successfully.")
        return True, None

    except Exception as e:
        return False, f"Unhandled exception during PostgreSQL setup: {e}"
