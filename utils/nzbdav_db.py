from utils.config_loader import CONFIG_MANAGER
from utils.global_logger import logger
from typing import Dict, Iterable, Optional, Tuple
import os, re, sqlite3

_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COLUMN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _get_config_path(config_dir: Optional[str] = None) -> str:
    if config_dir:
        return config_dir
    cfg = CONFIG_MANAGER.get("nzbdav", {})
    env_cfg = cfg.get("env", {}) if isinstance(cfg, dict) else {}
    return env_cfg.get("CONFIG_PATH") or cfg.get("config_dir") or "/nzbdav"


def _get_db_path(config_dir: Optional[str] = None) -> str:
    return os.path.join(_get_config_path(config_dir), "db.sqlite")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def list_tables(config_dir: Optional[str] = None) -> list[str]:
    db_path = _get_db_path(config_dir)
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"NzbDAV db not found: {db_path}")
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    return [row["name"] for row in rows]


def _validate_table_name(conn: sqlite3.Connection, table: str) -> str:
    if not isinstance(table, str) or not _TABLE_NAME_RE.match(table):
        raise ValueError(f"Invalid table name: {table!r}")

    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown table name: {table!r}")

    return table


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _validate_column_name(conn: sqlite3.Connection, table: str, column: str) -> str:
    if not isinstance(column, str) or not _COLUMN_NAME_RE.match(column):
        raise ValueError(f"Invalid column name: {column!r}")

    if column not in _get_table_column_names(conn, table):
        raise ValueError(f"Unknown column name: {column!r}")

    return column


def _get_table_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def get_table_columns(table: str, config_dir: Optional[str] = None) -> list[dict]:
    db_path = _get_db_path(config_dir)
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"NzbDAV db not found: {db_path}")
    with _connect(db_path) as conn:
        _validate_table_name(conn, table)
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [
        {
            "name": row["name"],
            "type": row["type"],
            "notnull": bool(row["notnull"]),
            "default": row["dflt_value"],
            "pk": bool(row["pk"]),
        }
        for row in rows
    ]


def list_primary_keys(table: str, config_dir: Optional[str] = None) -> list[str]:
    cols = get_table_columns(table, config_dir=config_dir)
    return [col["name"] for col in cols if col["pk"]]


def fetch_rows(
    table: str,
    config_dir: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    db_path = _get_db_path(config_dir)
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"NzbDAV db not found: {db_path}")
    if not isinstance(limit, int) or not isinstance(offset, int):
        raise TypeError("limit and offset must be integers.")
    if limit < 0 or offset < 0:
        raise ValueError("limit and offset must be non-negative integers.")
    with _connect(db_path) as conn:
        _validate_table_name(conn, table)
        rows = conn.execute(
            f"SELECT * FROM {table} LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_row(
    table: str,
    data: Dict[str, object],
    config_dir: Optional[str] = None,
    key_columns: Optional[Iterable[str]] = None,
) -> Tuple[bool, Optional[str]]:
    db_path = _get_db_path(config_dir)
    if not os.path.exists(db_path):
        return False, f"NzbDAV db not found: {db_path}"
    if not isinstance(data, dict):
        return False, "Data must be a mapping of column names to values."
    if not data:
        return False, "No data provided for upsert."

    with _connect(db_path) as conn:
        table = _validate_table_name(conn, table)
        table_columns = _get_table_column_names(conn, table)

        if key_columns is not None:
            if isinstance(key_columns, str) or not isinstance(key_columns, Iterable):
                raise ValueError("key_columns must be a non-empty iterable of strings.")
            keys = list(key_columns)
            if not keys:
                raise ValueError(f"No key columns provided for table {table}.")
        else:
            keys = list_primary_keys(table, config_dir=config_dir)

        if not keys:
            return False, f"No primary keys found for table {table}."

        for key in keys:
            if not isinstance(key, str):
                raise ValueError(f"Invalid key column type: {key!r}")
            _validate_column_name(conn, table, key)
        for column in data.keys():
            _validate_column_name(conn, table, column)
        for key in keys:
            if key not in data:
                return False, f"Missing key column in data: {key!r}"

        for column in data.keys():
            if column not in table_columns:
                return False, f"Unknown column name: {column!r}"

        columns = list(data.keys())
        placeholders = ", ".join(["?"] * len(columns))
        quoted_columns = [_quote_identifier(column) for column in columns]
        col_list = ", ".join(quoted_columns)

        update_cols = [c for c in columns if c not in keys]
        if update_cols:
            update_stmt = ", ".join(
                [
                    f"{_quote_identifier(c)}=excluded.{_quote_identifier(c)}"
                    for c in update_cols
                ]
            )
        else:
            update_stmt = ", ".join(
                [
                    f"{_quote_identifier(k)}=excluded.{_quote_identifier(k)}"
                    for k in keys
                ]
            )

        sql = (
            f"INSERT INTO {_quote_identifier(table)} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT({', '.join(_quote_identifier(key) for key in keys)}) DO UPDATE SET {update_stmt}"
        )
        try:
            conn.execute(sql, tuple(data.values()))
            conn.commit()
            return True, None
        except sqlite3.Error as e:
            logger.error("Failed to upsert into %s: %s", table, e)
            return False, str(e)


def delete_rows(
    table: str,
    where_sql: str,
    params: Optional[Iterable[object]] = None,
    config_dir: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    db_path = _get_db_path(config_dir)
    if not os.path.exists(db_path):
        return False, f"NzbDAV db not found: {db_path}"
    if not where_sql.strip():
        return False, "Refusing to delete without a WHERE clause."
    if len(where_sql) > 500:
        return False, "WHERE clause is too long."
    try:
        with _connect(db_path) as conn:
            _validate_table_name(conn, table)
            conn.execute(f"DELETE FROM {table} WHERE {where_sql}", params or [])
            conn.commit()
        return True, None
    except sqlite3.Error as e:
        logger.error("Failed to delete from %s: %s", table, e)
        return False, str(e)


def _find_backend_config_item_path(config_dir: Optional[str] = None) -> Optional[str]:
    base = _get_config_path(config_dir)
    candidates = [
        os.path.join(base, "backend", "Database", "Models", "ConfigItem.cs"),
        os.path.join(
            "/data", "nzbdav", "backend", "Database", "Models", "ConfigItem.cs"
        ),
        os.path.join("/nzbdav", "backend", "Database", "Models", "ConfigItem.cs"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def get_config_items(config_dir: Optional[str] = None) -> Dict[str, str]:
    db_path = _get_db_path(config_dir)
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"NzbDAV db not found: {db_path}")
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ConfigName, ConfigValue FROM ConfigItems ORDER BY ConfigName"
        ).fetchall()
    return {row["ConfigName"]: row["ConfigValue"] for row in rows}


def list_config_names(config_dir: Optional[str] = None) -> list[str]:
    return sorted(get_config_items(config_dir).keys())


def dump_config_items(config_dir: Optional[str] = None) -> list[dict]:
    db_path = _get_db_path(config_dir)
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"NzbDAV db not found: {db_path}")
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ConfigName, ConfigValue FROM ConfigItems ORDER BY ConfigName"
        ).fetchall()
    return [{"name": row["ConfigName"], "value": row["ConfigValue"]} for row in rows]


def list_known_config_names(config_dir: Optional[str] = None) -> list[str]:
    config_item_path = _find_backend_config_item_path(config_dir)
    if not config_item_path:
        raise FileNotFoundError("NzbDAV ConfigItem.cs not found.")
    try:
        with open(config_item_path, "r") as f:
            contents = f.read()
    except OSError as e:
        raise FileNotFoundError(f"Failed to read {config_item_path}: {e}") from e
    keys = set(re.findall(r'"([^"]+)"', contents))
    return sorted(keys)


def list_accounts(config_dir: Optional[str] = None) -> list[dict]:
    db_path = _get_db_path(config_dir)
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"NzbDAV db not found: {db_path}")
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT Type, Username FROM Accounts ORDER BY Type, Username"
        ).fetchall()
    return [{"type": row["Type"], "username": row["Username"]} for row in rows]


def get_config_value(name: str, config_dir: Optional[str] = None) -> Optional[str]:
    db_path = _get_db_path(config_dir)
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"NzbDAV db not found: {db_path}")
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT ConfigValue FROM ConfigItems WHERE ConfigName = ?",
            (name,),
        ).fetchone()
    return row["ConfigValue"] if row else None


def set_config_value(
    name: str, value: str, config_dir: Optional[str] = None
) -> Tuple[bool, Optional[str]]:
    db_path = _get_db_path(config_dir)
    if not os.path.exists(db_path):
        return False, f"NzbDAV db not found: {db_path}"
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "INSERT INTO ConfigItems (ConfigName, ConfigValue) "
                "VALUES (?, ?) "
                "ON CONFLICT(ConfigName) DO UPDATE SET ConfigValue = excluded.ConfigValue",
                (name, value),
            )
            conn.commit()
        return True, None
    except sqlite3.Error as e:
        logger.error("Failed to update NzbDAV config: %s", e)
        return False, str(e)
