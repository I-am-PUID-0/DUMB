import json
import os
import shlex
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed


def frontend_entrypoint_exists(frontend_config: dict) -> bool:
    config_dir = frontend_config.get("config_dir")
    if not isinstance(config_dir, str) or not config_dir.strip():
        return False

    command = frontend_config.get("command") or []
    if isinstance(command, str):
        try:
            command = shlex.split(command)
        except ValueError:
            return False
    if not isinstance(command, (list, tuple)) or not command:
        return False

    script_extensions = {".cjs", ".js", ".mjs"}
    for argument in command[1:]:
        if not isinstance(argument, str) or argument.startswith("-"):
            continue
        if os.path.splitext(argument)[1].lower() not in script_extensions:
            continue
        entrypoint = (
            argument if os.path.isabs(argument) else os.path.join(config_dir, argument)
        )
        return os.path.isfile(entrypoint)

    return os.path.isfile(os.path.join(config_dir, ".output/server/index.mjs"))


def frontend_start_readiness(frontend_config: dict) -> tuple[bool, str | None]:
    if not isinstance(frontend_config, dict) or not frontend_config.get("enabled"):
        return False, None
    if not frontend_entrypoint_exists(frontend_config):
        return False, "DUMB Frontend runtime artifacts are missing."
    if str(frontend_config.get("commit_sha") or "").strip():
        return False, "DUMB Frontend commit installation is enabled."
    if frontend_config.get("branch_enabled"):
        return False, "DUMB Frontend branch installation is enabled."
    if not frontend_config.get("release_version_enabled"):
        return True, None

    requested_version = str(frontend_config.get("release_version") or "").strip()
    if not requested_version or requested_version.lower() in {
        "latest",
        "nightly",
        "prerelease",
    }:
        return False, "DUMB Frontend release resolution is required."

    package_path = os.path.join(frontend_config["config_dir"], "package.json")
    try:
        with open(package_path, encoding="utf-8") as package_file:
            installed_version = str(json.load(package_file).get("version") or "")
    except (OSError, json.JSONDecodeError, AttributeError):
        return False, "DUMB Frontend installed version could not be verified."

    if installed_version.lstrip("v") != requested_version.lstrip("v"):
        return (
            False,
            f"DUMB Frontend {requested_version} is requested but "
            f"{installed_version or 'an unknown version'} is installed.",
        )
    return True, None


def start_control_plane_before_preinstall(
    *,
    api_enabled: bool,
    start_api: Callable[[], None],
    frontend_config: dict,
    start_frontend: Callable[[], bool],
    preinstall_services: Callable[[], None],
    frontend_deferred: Callable[[str], None] | None = None,
    frontend_starting_early: Callable[[], None] | None = None,
) -> None:
    if api_enabled:
        start_api()

    frontend_ready, reason = frontend_start_readiness(frontend_config)
    frontend_started_early = False
    if frontend_ready:
        if frontend_starting_early:
            frontend_starting_early()
        frontend_started_early = bool(start_frontend())
    elif reason and frontend_deferred:
        frontend_deferred(reason)

    preinstall_services()

    if not frontend_started_early:
        start_frontend()


def run_parallel_preinstall(
    targets: list[tuple[str, str]],
    install_target: Callable[[str, str], None],
    max_workers: int = 4,
) -> dict[str, str]:
    if not targets:
        return {}

    failures = {}
    worker_count = min(max_workers, max(1, len(targets)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(install_target, key, name): name for key, name in targets
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                future.result()
            except Exception as error:
                failures[name] = str(error)
    return failures
