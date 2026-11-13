"""Resolve PostgreSQL's two managed path tokens for every process launch.

Tokenize string templates before substitution so a path containing spaces or
quotes stays inside its original argument. Keep persisted templates unchanged.
"""

import re
import shlex

_PATH_TOKEN = re.compile(r"\{postgres_config_(?:dir|file)\}")


def postgres_launch_command(command, config_dir, config_file):
    """Return fresh argv, replacing only known path tokens once."""
    try:
        if isinstance(command, str):
            arguments = shlex.split(command)
        elif isinstance(command, (list, tuple)):
            arguments = list(command)
        else:
            raise TypeError
    except (TypeError, ValueError):
        raise ValueError(
            "The PostgreSQL launch command must be a quoted string or string argument list."
        ) from None
    if not arguments or any(
        not isinstance(argument, str) or "\x00" in argument for argument in arguments
    ):
        raise ValueError(
            "The PostgreSQL launch command must be a quoted string or string argument list."
        )
    paths = {
        "{postgres_config_dir}": config_dir,
        "{postgres_config_file}": config_file,
    }

    def replace(match):
        value = paths[match.group()]
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError(
                "A PostgreSQL launch path token requires its configured nonempty path."
            )
        return value

    return [_PATH_TOKEN.sub(replace, argument) for argument in arguments]
