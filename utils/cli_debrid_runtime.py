"""Installed CLI Debrid topology and narrowly owned writable runtime paths."""

import ast
import os
from pathlib import Path
import shlex
import stat


def cli_battery_is_managed(cli_config, battery_config):
    """Leave separately configured Battery commands and source trees alone."""
    if not isinstance(cli_config, dict) or not isinstance(battery_config, dict):
        return False
    root = cli_config.get("config_dir")
    battery_root = battery_config.get("config_dir")
    if (
        not root
        or not battery_root
        or os.path.realpath(root) != os.path.realpath(battery_root)
    ):
        return False
    command = battery_config.get("command") or []
    if isinstance(command, str):
        try:
            command = shlex.split(command)
        except ValueError:
            return False
    return (
        isinstance(command, (list, tuple))
        and len(command) == 2
        and all(isinstance(part, str) for part in command)
        and os.path.abspath(command[0])
        == os.path.abspath(os.path.join(root, "venv", "bin", "python"))
        and os.path.abspath(os.path.join(battery_root, command[1]))
        == os.path.abspath(os.path.join(root, "cli_battery", "main.py"))
    )


def cli_battery_topology(cli_config):
    """Classify installed files without importing or trusting version labels."""
    root = Path(cli_config.get("config_dir") or "/cli_debrid")
    legacy_entry = (root / "cli_battery" / "main.py").is_file()
    try:
        package = ast.parse(
            (root / "cli_battery" / "app" / "__init__.py").read_text(encoding="utf-8")
        )
        direct = ast.parse(
            (root / "cli_battery" / "app" / "direct_api.py").read_text(encoding="utf-8")
        )
    except (OSError, SyntaxError, UnicodeError):
        return "standalone" if legacy_entry else "unknown"
    exports_direct = any(
        isinstance(node, ast.ImportFrom)
        and node.level == 1
        and node.module == "direct_api"
        and any(alias.name == "DirectAPI" for alias in node.names)
        for node in package.body
    )
    legacy_app = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "create_app"
        for node in package.body
    )
    if (
        exports_direct
        and not legacy_app
        and any(
            isinstance(node, ast.ClassDef) and node.name == "DirectAPI"
            for node in direct.body
        )
    ):
        return "embedded"
    return "standalone" if legacy_entry else "unknown"


def cli_battery_is_embedded(cli_config, battery_config):
    return (
        cli_battery_is_managed(cli_config, battery_config)
        and cli_battery_topology(cli_config) == "embedded"
    )


def _own_entry(path, uid, gid):
    info = path.lstat()
    if (info.st_uid, info.st_gid) != (uid, gid):
        os.chown(path, uid, gid, follow_symlinks=False)


def _create_directory(path, uid, gid, *, own_existing=False):
    missing = []
    current = path
    while not current.exists():
        if current.is_symlink():
            raise ValueError(f"Refusing dangling directory link: {current}")
        missing.append(current)
        current = current.parent
    if not current.is_dir():
        raise ValueError(f"Not a directory: {current}")
    for directory in reversed(missing):
        directory.mkdir()
        _own_entry(directory, uid, gid)
    if own_existing and not path.is_symlink():
        _own_entry(path, uid, gid)


def _validate_data_leaf(root, relative):
    leaf = root / relative
    current = root
    for component in Path(relative).parts:
        current = current / component
        if current.is_symlink() or (current.exists() and os.path.ismount(current)):
            raise ValueError(
                f"Refusing redirected CLI Debrid data directory: {current}"
            )
    if not leaf.exists():
        return
    if not leaf.is_dir():
        raise ValueError(f"CLI Debrid data directory is not a directory: {leaf}")
    device = leaf.stat().st_dev
    for directory, dirs, files in os.walk(leaf, followlinks=False):
        for name in [*dirs, *files]:
            entry = Path(directory) / name
            info = entry.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or info.st_dev != device
                or os.path.ismount(entry)
            ):
                raise ValueError(f"Refusing redirected CLI Debrid data entry: {entry}")


def _own_runtime_leaf(root, relative, uid, gid):
    """Own one fixed generated/source leaf; never follow child links or mounts."""
    leaf = root / relative
    current = root
    for component in Path(relative).parts:
        current = current / component
        if current.is_symlink() or (current.exists() and os.path.ismount(current)):
            raise ValueError(f"Refusing redirected CLI Debrid runtime leaf: {current}")
    _create_directory(leaf, uid, gid, own_existing=True)
    device = leaf.stat().st_dev
    for directory, dirs, files in os.walk(leaf, followlinks=False):
        dirs[:] = [
            name
            for name in dirs
            if not (Path(directory) / name).is_symlink()
            and not os.path.ismount(Path(directory) / name)
        ]
        for name in [*dirs, *files]:
            entry = Path(directory) / name
            info = entry.lstat()
            if (
                not stat.S_ISLNK(info.st_mode)
                and info.st_dev == device
                and not os.path.ismount(entry)
            ):
                _own_entry(entry, uid, gid)


def prepare_cli_debrid_runtime(config, data_root, uid, gid):
    """Restore required data alias after extraction, then own only writable leaves.

    Custom data paths remain authoritative. Existing foreign data and redirected
    aliases are refused rather than migrated, removed, or recursively re-owned.
    """
    root = Path(config.get("config_dir") or "/cli_debrid")
    alias = root / "data"
    env = config.get("env") or {}
    paths = [
        Path(str(env[key]))
        for key in ("USER_CONFIG", "USER_LOGS", "USER_DB_CONTENT")
        if env.get(key)
    ]
    paths.extend(
        Path(str(config[key])).parent
        for key in ("config_file", "log_file")
        if config.get(key)
    )
    alias_paths = [
        path for path in paths if path.is_absolute() and path.is_relative_to(alias)
    ]
    try:
        if alias_paths:
            data_root = Path(data_root or "/data")
            target = data_root / "cli_debrid"
            if (
                not data_root.is_absolute()
                or not data_root.is_dir()
                or target.is_relative_to(alias)
            ):
                raise ValueError(
                    "CLI Debrid requires an existing persistent data_root outside its runtime/data alias"
                )
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise ValueError(
                    f"Refusing redirected CLI Debrid persistent data: {target}"
                )
            if alias.is_symlink():
                if alias.resolve() != target.resolve():
                    raise ValueError(
                        f"Refusing redirected CLI Debrid data alias: {alias}"
                    )
            elif alias.exists():
                if not alias.is_dir() or os.path.ismount(alias) or any(alias.iterdir()):
                    raise ValueError(
                        f"Refusing to replace existing CLI Debrid data: {alias}"
                    )
            for path in alias_paths:
                if not path.resolve().is_relative_to(target.resolve()):
                    # Before creating a missing alias, resolve its relative suffix
                    # against the intended target instead of the absent alias.
                    candidate = target / path.relative_to(alias)
                    if not candidate.resolve().is_relative_to(target.resolve()):
                        raise ValueError(
                            f"Refusing redirected CLI Debrid data directory: {path}"
                        )
                _validate_data_leaf(target, path.relative_to(alias))
            _create_directory(target, uid, gid, own_existing=True)
            if not alias.is_symlink():
                if alias.exists():
                    alias.rmdir()  # Only the empty directory checked above.
                alias.symlink_to(target, target_is_directory=True)
            for path in alias_paths:
                _own_runtime_leaf(target, path.relative_to(alias), uid, gid)
        for path in paths:
            if path.is_absolute() and path not in alias_paths:
                _create_directory(path, uid, gid)
        if (root / "utilities").is_dir():
            _own_runtime_leaf(root, "utilities", uid, gid)
        if (root / "database" / "collection_poster_renderer.py").is_file():
            _own_runtime_leaf(root, "static/img/collection_poster_previews", uid, gid)
    except (OSError, ValueError) as error:
        return False, f"CLI Debrid runtime preparation failed: {error}"
    return True, None
