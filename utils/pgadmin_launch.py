"""Resolve only DUMB's portable pgAdmin web command without invoking a shell."""

from pathlib import Path
import shlex

PGADMIN_VENV = "/pgadmin/venv"
_WEB_SCRIPT = "lib/python*/site-packages/pgadmin4/pgAdmin4.py"


def pgadmin_command_template(venv_dir=None):
    root = Path(venv_dir or PGADMIN_VENV)
    return [str(root / "bin/python3"), str(root / _WEB_SCRIPT)]


def pgadmin_launch_command(command, venv_dir=None):
    """Keep custom argv literal; resolve one known managed entrypoint token."""
    if isinstance(command, str):
        argv = shlex.split(command)
    elif isinstance(command, (list, tuple)):
        argv = list(command)
    else:
        raise ValueError(
            "The pgAdmin startup command must be a string or argument list."
        )
    if not argv or any(not isinstance(argument, str) for argument in argv):
        raise ValueError("The pgAdmin startup command requires string arguments.")
    template = pgadmin_command_template(venv_dir)
    if len(argv) >= 2 and argv[:2] == template:
        root = Path(template[0]).parent.parent
        scripts = list(root.glob(_WEB_SCRIPT))
        if (
            not Path(template[0]).is_file()
            or len(scripts) != 1
            or not scripts[0].is_file()
        ):
            raise ValueError(
                "The installed pgAdmin Python runtime or unique web entrypoint is unavailable."
            )
        argv[1] = str(scripts[0])
    return argv
