"""Add pgAdmin server definitions through its documented, installed setup CLI.

Contract: pgAdmin 4 9.18 import_export_servers.html and web/setup.py at
pgadmin-org/pgadmin4 e8f7a18c98e4160193a676e36330902dcedfa519. Never write the
internal config database or use --replace. CLI success alone is insufficient:
several upstream commands catch errors without returning a failing exit status.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading

from utils.private_files import atomic_write_private_text

_LOCK = threading.Lock()


class PgAdminSetupError(ValueError):
    pass


def schema_fields(resource, config, payload=None):
    del config, payload
    if resource.get("service_key") != "pgadmin":
        return []
    return [
        {
            "key": f"{resource['id']}::{key}",
            "resource_id": resource["id"],
            "service_key": "pgadmin",
            "type": "string",
            "label": label,
            "required": False,
            "description": description,
        }
        for key, label, description in (
            (
                "server_name",
                "PostgreSQL connection name",
                "Defaults to the configured PostgreSQL user. An existing connection with different settings is preserved.",
            ),
            (
                "server_group",
                "pgAdmin server group",
                "Defaults to DUMB. Existing groups and servers are preserved. The database password is entered at first connection in pgAdmin.",
            ),
        )
    ]


def _text(value, label):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 256
        or any(ord(c) < 32 for c in value)
    ):
        raise PgAdminSetupError(f"Provide a valid {label}.")
    return value.strip()


def _installed_cli(cfg):
    # The executable and setup script must come from the SAME installed runtime.
    root = Path(cfg.get("venv_dir") or "/pgadmin/venv")
    python = root / "bin/python3"
    scripts = list(root.glob("lib/python*/site-packages/pgadmin4/setup.py"))
    if not python.is_file() or len(scripts) != 1:
        raise PgAdminSetupError(
            "Wait for the installed pgAdmin runtime and its supported setup.py command."
        )
    return str(python), str(scripts[0])


def _run(cli, command, path, user, runner, owner=None):
    # No shell, credentials on argv, inherited libpq passwords or captured output
    # in an exception. pgAdmin reads its existing config_local.py itself.
    env = os.environ.copy()
    for key in ("PGPASSWORD", "PGADMIN_SETUP_PASSWORD", "PGADMIN_DEFAULT_PASSWORD"):
        env.pop(key, None)
    result = runner(
        [*cli, command, str(path), "--user", user],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
        **(
            {"user": owner[0], "group": owner[1]}
            if owner is not None and os.geteuid() == 0
            else {}
        ),
    )
    if result.returncode != 0:
        raise PgAdminSetupError(
            "pgAdmin's supported server import/export command failed. Check its administrator account and configuration readiness."
        )


def _dump(cli, directory, user, runner, owner=None):
    path = Path(directory) / "export.json"
    path.unlink(missing_ok=True)
    # Precreate privately: exports can contain private endpoints and commands.
    atomic_write_private_text(path, "", owner=owner)
    _run(cli, "dump-servers", path, user, runner, owner)
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError
        document = json.loads(path.read_text())
        servers = document["Servers"]
        if not isinstance(servers, dict) or any(
            not isinstance(row, dict) for row in servers.values()
        ):
            raise ValueError
        return servers
    except (OSError, ValueError, KeyError, TypeError):
        raise PgAdminSetupError(
            "pgAdmin did not export a supported server list for the configured administrator. Finish its first account setup, then retry."
        ) from None


def _matches(row, desired):
    return all(
        row.get(key) == value
        for key, value in desired.items()
        if key not in {"ConnectionParameters", "Comment"}
    )


def ensure_server(cfg, details, *, apply=False, runner=subprocess.run, cli=None):
    """Return True if present, False if preview needs an append-only import."""
    user = _text(cfg.get("setup_email"), "pgAdmin administrator email")
    desired = {
        "Name": _text(details.get("name"), "server name"),
        "Group": _text(details.get("group") or "DUMB", "server group"),
        "Host": _text(details.get("host"), "PostgreSQL host"),
        "Port": int(details.get("port") or 5432),
        "Username": _text(details.get("username"), "PostgreSQL user"),
        "MaintenanceDB": _text(
            details.get("maintenance_db") or "postgres", "maintenance database"
        ),
        "ConnectionParameters": {"sslmode": "prefer", "connect_timeout": 10},
        "Comment": "Added by DUMB using pgAdmin's supported server import.",
    }
    if not 1 <= desired["Port"] <= 65535:
        raise PgAdminSetupError("Use a valid PostgreSQL port.")
    with _LOCK, tempfile.TemporaryDirectory(prefix="dumb-pgadmin-import-") as directory:
        owner = None
        if cfg.get("puid") is not None and cfg.get("pgid") is not None:
            owner = int(cfg["puid"]), int(cfg["pgid"])
            if min(owner) < 0:
                raise PgAdminSetupError(
                    "Use nonnegative configured pgAdmin service IDs."
                )
            if os.geteuid() == 0:
                os.chown(directory, *owner)
            elif owner != (os.geteuid(), os.getegid()):
                raise PgAdminSetupError(
                    "Run pgAdmin provisioning as its configured service user."
                )
        installed = cli or _installed_cli(cfg)
        before = _dump(installed, directory, user, runner, owner)
        matches = [
            row
            for row in before.values()
            if all(
                row.get(key) == desired[key]
                for key in ("Host", "Port", "Username", "MaintenanceDB")
            )
        ]
        if matches:
            return True
        if any(
            row.get("Name") == desired["Name"] and row.get("Group") == desired["Group"]
            for row in before.values()
        ):
            raise PgAdminSetupError(
                "The selected pgAdmin name/group already belongs to a different connection. Choose a new name; existing settings and saved passwords were preserved."
            )
        if not apply:
            return False
        path = Path(directory) / "import.json"
        atomic_write_private_text(
            path, json.dumps({"Servers": {"1": desired}}), owner=owner
        )
        _run(installed, "load-servers", path, user, runner, owner)
        after = _dump(installed, directory, user, runner, owner)
        if not any(_matches(row, desired) for row in after.values()):
            raise PgAdminSetupError(
                "pgAdmin did not retain the imported PostgreSQL connection. Its CLI can exit successfully without importing; retry after administrator setup completes."
            )
        # Export IDs are sequential and can change; compare complete rows instead.
        remaining = list(after.values())
        for row in before.values():
            if row not in remaining:
                raise PgAdminSetupError(
                    "The pgAdmin readback changed an existing connection. Review the server list before retrying."
                )
            remaining.remove(row)
        return True


def _state(execution, reason, *, verification="not_checked", retryable=False):
    return {
        "execution": execution,
        "verification": verification,
        "reason": reason,
        "retryable": retryable,
        "evidence": [],
    }


def _dispatch(action, config, payload, *, apply, runner, cli):
    if (
        action.get("service_key") != "pgadmin"
        or action.get("action") != "postgres_connection"
    ):
        return None
    cfg = {
        **(config.get("pgadmin") or {}),
        "puid": config.get("puid"),
        "pgid": config.get("pgid"),
    }
    postgres = config.get("postgres") or {}
    if not postgres.get("enabled"):
        return _state(
            "waiting_input",
            "Select managed PostgreSQL before adding its pgAdmin connection.",
        )
    values = (payload.get("arrangement") or {}).get("values") or {}
    source = action["source"]
    details = {
        "name": values.get(f"{source}::server_name") or postgres.get("user"),
        "group": values.get(f"{source}::server_group") or "DUMB",
        "host": postgres.get("host") or "127.0.0.1",
        "port": postgres.get("port") or 5432,
        "username": postgres.get("user"),
        "maintenance_db": "postgres",
    }
    try:
        if not apply:
            _text(cfg.get("setup_email"), "pgAdmin administrator email")
            _text(details["name"], "server name")
            _text(details["group"], "server group")
            _text(details["username"], "PostgreSQL user")
            if not cli:
                _installed_cli(cfg)
            return _state(
                "planned",
                "Apply to inspect and register the selected PostgreSQL connection through pgAdmin's installed import/export command. Preview does not initialize its configuration database.",
            )
        present = ensure_server(cfg, details, apply=False, runner=runner, cli=cli)
        if present:
            return _state(
                "already_present",
                "The selected PostgreSQL connection is registered in pgAdmin. Enter its database password at first connection.",
                verification="passed",
            )
        if not apply:
            return _state(
                "planned",
                "Add the selected PostgreSQL connection using pgAdmin's supported import; existing servers are preserved.",
            )
        ensure_server(cfg, details, apply=True, runner=runner, cli=cli)
        return _state(
            "applied",
            "Registered and read back the PostgreSQL connection. Enter its database password at first connection in pgAdmin.",
            verification="passed",
        )
    except PgAdminSetupError as exc:
        return _state("waiting_input", str(exc))
    except (OSError, subprocess.SubprocessError):
        return _state(
            "waiting_dependency",
            "Wait for pgAdmin's configuration database and administrator account to become ready, then retry the supported import.",
            retryable=True,
        )
    except (ValueError, TypeError):
        return _state(
            "waiting_input",
            "Check the pgAdmin administrator and selected PostgreSQL connection fields.",
        )


def inspect_pgadmin(
    action,
    config,
    payload,
    operation=None,
    *,
    runner=subprocess.run,
    cli=None,
    **kwargs,
):
    del operation, kwargs
    return _dispatch(action, config, payload, apply=False, runner=runner, cli=cli)


def apply_pgadmin(
    action,
    config,
    payload,
    operation=None,
    *,
    runner=subprocess.run,
    cli=None,
    **kwargs,
):
    del operation, kwargs
    return _dispatch(action, config, payload, apply=True, runner=runner, cli=cli)
