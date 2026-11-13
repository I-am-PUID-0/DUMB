"""Verify download-client credential continuity without publishing credentials.

The native administrator key is separate from the SAB key used by Arr clients.
Read only the retained source and stopped private candidate; never rewrite
existing clients, settings or source databases to make verification succeed.
"""

from contextlib import closing
import hmac
import json
from pathlib import Path
import sqlite3
import stat
from urllib.parse import quote

MAX_KEY = 8192
MAX_SAB_SETTINGS = 65536


class MigrationCredentialError(ValueError):
    """A static, credential-free explanation of refused import acceptance."""


def _key(value, *, trim=False):
    if value is None:
        return ""
    value = str(value)
    if trim:
        value = value.strip()
    if len(value) > MAX_KEY or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise MigrationCredentialError(
            "The download-client API key is not a bounded valid credential; review the retained v1 settings."
        )
    return value


def _override(environment, name, *, native=False):
    values = []
    for key, value in (environment or {}).items():
        if not isinstance(key, str):
            continue
        matches = (
            key.startswith("INFINIDYSK__") and key.lower() == name.lower()
            if native
            else key.lower() == name.lower()
        )
        if matches:
            values.append(_key(value, trim=not native))
    if len(set(values)) > 1:
        raise MigrationCredentialError(
            "Conflicting download-client API-key environment aliases require review before migration."
        )
    return bool(values), values[0] if values else ""


def legacy_sab_environment(environment):
    present, value = _override(environment, "NZBDAV_CONFIG__API__KEY")
    return {"NZBDAV_CONFIG__API__KEY": value} if present else {}


def _read_setting(database, table, name_column, value_column, name, limit):
    path = Path(database)
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise MigrationCredentialError(
                "Download-client credential verification requires the retained regular database."
            )
        # Identifiers are fixed by the two callers; only the setting name is data.
        query = (
            f'SELECT substr("{value_column}",1,?), length("{value_column}") '
            f'FROM "{table}" WHERE "{name_column}"=? LIMIT 2'
        )
        with closing(
            sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
        ) as db:
            rows = db.execute(query, (limit + 1, name)).fetchall()
        after = path.lstat()
        stamp = lambda row: (
            row.st_dev,
            row.st_ino,
            row.st_nlink,
            row.st_size,
            row.st_mtime_ns,
            row.st_ctime_ns,
        )
        if stamp(before) != stamp(after) or len(rows) > 1:
            raise MigrationCredentialError(
                "Download-client settings changed or are ambiguous; migration acceptance was refused."
            )
        if not rows:
            return None
        value, size = rows[0]
        if not isinstance(value, str) or type(size) is not int or size > limit:
            raise MigrationCredentialError(
                "Download-client settings exceed the supported credential metadata shape."
            )
        return value
    except (OSError, sqlite3.Error):
        raise MigrationCredentialError(
            "The retained source or native download-client settings could not be inspected; no credentials were changed."
        ) from None


def source_sab_key(database, environment):
    present, overlay = _override(environment, "NZBDAV_CONFIG__API__KEY")
    stored = _read_setting(
        database, "ConfigItems", "ConfigName", "ConfigValue", "api.key", MAX_KEY
    )
    # Match pinned native Config::read/get: overlay, stored value, old fallback.
    result = (overlay if present else _key(stored, trim=True)) or _key(
        (environment or {}).get("FRONTEND_BACKEND_API_KEY"), trim=True
    )
    if not result:
        raise MigrationCredentialError(
            "The existing v1 download-client API key could not be established. Review its API settings before migrating; DUMB will not silently generate a replacement."
        )
    return result


def validate_target_sab_override(expected, environment):
    # Native also accepts a JSON override for the entire SAB section. It can
    # replace api_key independently of the stored credential and leaf alias.
    if any(
        isinstance(name, str)
        and name.startswith("INFINIDYSK__")
        and name.lower() == "infinidysk__sab"
        for name in (environment or {})
    ):
        raise MigrationCredentialError(
            "A v2 SAB section environment override can replace the existing download-client credential. Use individual SAB settings before migrating."
        )
    present, override = _override(environment, "INFINIDYSK__SAB__API_KEY", native=True)
    if present and not hmac.compare_digest(override.encode(), expected.encode()):
        raise MigrationCredentialError(
            "A v2 SAB API-key environment override would change the existing download-client credential. Remove or correct that override before migration."
        )


def verify_imported_sab_key(database, expected, environment):
    validate_target_sab_override(expected, environment)
    raw = _read_setting(database, "config", "key", "value", "sab", MAX_SAB_SETTINGS)
    try:
        settings = json.loads(raw) if isinstance(raw, str) else None
        value = settings.get("api_key") if isinstance(settings, dict) else None
        actual = _key(value) if isinstance(value, str) else ""
    except (ValueError, TypeError):
        raise MigrationCredentialError(
            "The imported native SAB credential could not be verified; migration acceptance was refused."
        ) from None
    if not actual or not hmac.compare_digest(actual.encode(), expected.encode()):
        raise MigrationCredentialError(
            "The native import did not preserve the existing download-client API key. Cutover was refused; retained v1 data and Arr client settings were not changed."
        )
    return {"download_credentials_verified": True}
