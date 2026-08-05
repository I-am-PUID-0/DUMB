#!/usr/bin/env python3
"""Run one destructive service update regression in a disposable DUMB container.

The runner never mounts the active DUMB ``/config`` or ``/data`` trees. It
installs a requested older release into fresh state, waits for DUMB startup,
invokes the real manual-update API with ``Override + latest``, verifies service
health, and removes the container and temporary state.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

DEFAULT_IMAGE = "dumb-regression-base:local"
TERMINAL_PHASES = {"ready", "degraded"}


def _run(command: list[str], *, check: bool = True, capture: bool = True):
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=capture,
    )


def _disable_services(config: dict) -> None:
    for key, value in config.items():
        if key == "dumb" or not isinstance(value, dict):
            continue
        instances = value.get("instances")
        if isinstance(instances, dict):
            for instance in instances.values():
                if isinstance(instance, dict) and "enabled" in instance:
                    instance["enabled"] = False
        elif "enabled" in value:
            value["enabled"] = False


def _service_config(config: dict, key: str, instance_name: str | None) -> dict:
    service = config.get(key)
    if not isinstance(service, dict):
        raise ValueError(f"Unknown service key: {key}")
    instances = service.get("instances")
    if not isinstance(instances, dict):
        if instance_name:
            raise ValueError(f"{key} is not an instance-based service")
        return service
    if not instance_name:
        raise ValueError(f"{key} requires --instance; choices: {', '.join(instances)}")
    instance = instances.get(instance_name)
    if not isinstance(instance, dict):
        raise ValueError(
            f"Unknown {key} instance {instance_name!r}; choices: {', '.join(instances)}"
        )
    return instance


def _deep_merge_config(target: dict, updates: dict) -> None:
    """Merge a matrix-owned disposable fixture without replacing siblings."""

    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge_config(target[key], value)
        else:
            target[key] = value


def prepare_config(
    template_path: Path,
    destination: Path,
    *,
    key: str,
    instance_name: str | None,
    previous_version: str,
    selector: str,
    dependencies: list[str],
    config_overrides: dict | None = None,
    github_token: str | None = None,
) -> tuple[str, Path]:
    with template_path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    _disable_services(config)

    target = _service_config(config, key, instance_name)
    target["enabled"] = True
    target["auto_update"] = False
    target["commit_sha"] = ""
    target["branch_enabled"] = False
    if selector == "release":
        target["release_version_enabled"] = True
        target["release_version"] = previous_version
        target["pinned_version"] = ""
    else:
        target["pinned_version"] = previous_version
        if "release_version_enabled" in target:
            target["release_version_enabled"] = False

    # A clean Decypharr regression must not require real provider credentials.
    if key == "decypharr":
        target["mount_type"] = "dfs"
        target["api_keys"] = {}
    elif key == "authelia":
        target["public_url"] = "https://auth.example.com"
        target["cookie_domain"] = "example.com"
        target["default_redirection_url"] = "https://dumb.example.com"

    for dependency_key in dependencies:
        dependency = config.get(dependency_key)
        if not isinstance(dependency, dict) or "enabled" not in dependency:
            raise ValueError(f"Unsupported dependency key: {dependency_key}")
        dependency["enabled"] = True

    if config_overrides:
        _deep_merge_config(config, config_overrides)

    dumb = config.setdefault("dumb", {})
    dumb["github_rate_limit_max_wait_seconds"] = 0
    if github_token:
        dumb["github_token"] = github_token
    dumb.setdefault("startup", {}).update(
        readiness_timeout_seconds=300,
        stabilization_seconds=5,
    )
    dumb.setdefault("install_cache", {}).update(
        activation_health_timeout_seconds=180,
        activation_stabilization_seconds=5,
    )
    dumb.setdefault("media_protection", {})["enabled"] = False

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
    destination.chmod(0o600)
    if key == "authelia":
        users_path = destination.parent / "authelia" / "users_database.yml"
        users_path.parent.mkdir(parents=True, exist_ok=True)
        users_path.write_text(
            "users:\n"
            "  regression:\n"
            "    disabled: false\n"
            "    displayname: Regression User\n"
            "    password: '$2b$12$JGvMSUzc4YV/KaxA6ityMejT5rrGEsCDFpx3opKmIHGK.XwBWhbCm'\n"
            "    email: user@example.com\n"
            "    groups:\n"
            "      - admins\n",
            encoding="utf-8",
        )
        users_path.chmod(0o600)
    process_name = str(target.get("process_name") or "").strip()
    if not process_name:
        raise ValueError(f"{key} has no process_name")
    config_dir = Path(str(target.get("config_dir") or ""))
    return process_name, config_dir


def _docker_exec_json(container: str, path: str, *, timeout: int = 10) -> dict:
    result = _run(
        [
            "docker",
            "exec",
            container,
            "curl",
            "-fsS",
            "--max-time",
            str(timeout),
            f"http://127.0.0.1:8000{path}",
        ]
    )
    return json.loads(result.stdout)


def _probe_service_urls(container: str, urls: list[str]) -> None:
    for url in urls:
        _run(
            [
                "docker",
                "exec",
                container,
                "curl",
                "-fsS",
                "--max-time",
                "10",
                url,
            ]
        )


def _container_is_running(container: str) -> bool:
    result = _run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container],
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _wait_for_startup(container: str, timeout_seconds: int) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict = {}
    while time.monotonic() < deadline:
        try:
            last_payload = _docker_exec_json(
                container, "/process/startup-status", timeout=2
            )
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            if not _container_is_running(container):
                raise RuntimeError("DUMB regression container exited during startup")
            time.sleep(2)
            continue
        if last_payload.get("phase") in TERMINAL_PHASES:
            return last_payload
        time.sleep(2)
    raise TimeoutError(
        f"DUMB startup did not reach a terminal phase: {last_payload or 'no status'}"
    )


def _set_configured_release_target(
    container: str, process_name: str, target_version: str
) -> None:
    encoded_name = urllib.parse.quote(process_name, safe="")
    service_config = _docker_exec_json(
        container, f"/config/?process_name={encoded_name}"
    )
    service_config["commit_sha"] = ""
    service_config["branch_enabled"] = False
    service_config["release_version_enabled"] = True
    service_config["release_version"] = target_version
    if "pinned_version" in service_config:
        service_config["pinned_version"] = ""
    payload = json.dumps(
        {
            "process_name": process_name,
            "updates": service_config,
            "persist": True,
        }
    )
    result = _run(
        [
            "docker",
            "exec",
            container,
            "curl",
            "-fsS",
            "--max-time",
            "30",
            "-H",
            "Content-Type: application/json",
            "-d",
            payload,
            "http://127.0.0.1:8000/config/",
        ]
    )
    response = json.loads(result.stdout)
    if response.get("status") != "service config updated":
        raise RuntimeError(f"Unable to configure update target: {response}")


def _post_update(
    container: str,
    process_name: str,
    timeout_seconds: int,
    *,
    target: str | None = "latest",
) -> dict:
    payload = json.dumps(
        {
            "process_name": process_name,
            "allow_override": target == "latest",
            "target": target,
        }
    )
    result = _run(
        [
            "docker",
            "exec",
            container,
            "curl",
            "-fsS",
            "--max-time",
            str(timeout_seconds),
            "-H",
            "Content-Type: application/json",
            "-d",
            payload,
            "http://127.0.0.1:8000/process/update-install",
        ]
    )
    return json.loads(result.stdout)


def _container_log_tail(container: str, lines: int = 160) -> str:
    result = _run(
        ["docker", "logs", "--tail", str(lines), container],
        check=False,
    )
    return (result.stdout or "") + (result.stderr or "")


def _remove_regression_state(regression_root: Path, image: str) -> None:
    """Remove disposable state even when container-created files are root-owned."""

    try:
        shutil.rmtree(regression_root)
    except FileNotFoundError:
        return
    except OSError:
        pass
    if not regression_root.exists():
        return
    if (
        regression_root.parent.resolve() != regression_root.parent
        or regression_root.name != Path(regression_root.name).name
        or not regression_root.name.startswith("dumb-update-regression-")
    ):
        raise ValueError(f"Refusing unsafe regression cleanup path: {regression_root}")

    cleanup_script = (
        "import shutil,sys; from pathlib import Path; "
        "parent=Path('/cleanup-parent').resolve(); name=sys.argv[1]; "
        "assert Path(name).name == name and name.startswith('dumb-update-regression-'); "
        "target=(parent/name).resolve(); assert target.parent == parent; "
        "shutil.rmtree(target)"
    )
    result = _run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{regression_root.parent}:/cleanup-parent",
            "--entrypoint",
            "/venv/bin/python",
            image,
            "-c",
            cleanup_script,
            regression_root.name,
        ],
        check=False,
    )
    if regression_root.exists():
        details = (result.stderr or result.stdout or "cleanup helper failed").strip()
        raise OSError(f"Unable to remove disposable regression state: {details}")


def run_regression(args: argparse.Namespace) -> dict:
    repo_root = Path(__file__).resolve().parents[1]
    shared_parent = repo_root.parent
    template_path = repo_root / "utils" / "dumb_config.json"
    regression_root = Path(
        tempfile.mkdtemp(
            prefix=f"dumb-update-regression-{args.key}-", dir=shared_parent
        )
    )
    container = f"dumb-regression-{args.key}-{os.getpid()}"
    config_root = regression_root / "config"
    data_root = regression_root / "data"
    log_root = regression_root / "log"
    mount_root = regression_root / "mnt" / "debrid"
    cache_root = Path(args.cache_dir).resolve() if args.cache_dir else None

    try:
        if args.mode == "install-only" and args.key != "zurg":
            raise ValueError("Install-only regression mode currently supports zurg")
        for path in (config_root, data_root, log_root, mount_root):
            path.mkdir(parents=True, exist_ok=True)
        process_name, config_dir = prepare_config(
            template_path,
            config_root / "dumb_config.json",
            key=args.key,
            instance_name=args.instance,
            previous_version=args.previous,
            selector=args.selector,
            dependencies=args.enable_dependency,
            config_overrides=args.config_overrides,
            github_token=os.environ.get("DUMB_REGRESSION_GITHUB_TOKEN"),
        )
        if cache_root:
            cache_root.mkdir(parents=True, exist_ok=True)

        command = [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--cap-add",
            "SYS_ADMIN",
            "--device",
            "/dev/fuse",
            "--entrypoint",
            "/venv/bin/python",
            "-v",
            f"{repo_root / 'main.py'}:/main.py:ro",
            "-v",
            f"{repo_root / 'utils'}:/utils:ro",
            "-v",
            f"{repo_root / 'api'}:/api:ro",
            "-v",
            f"{config_root}:/config",
            "-v",
            f"{data_root}:/data",
            "-v",
            f"{log_root}:/log",
            "-v",
            f"{mount_root}:/mnt/debrid:rshared",
        ]
        if cache_root:
            command.extend(["-v", f"{cache_root}:/config/.cache/dumb"])
        if args.mode == "install-only":
            command.extend(
                [
                    "-v",
                    f"{repo_root / 'scripts' / 'regression_install_only_service.py'}:"
                    "/regression_install_only_service.py:ro",
                ]
            )
            command.extend(
                [
                    args.image,
                    "/regression_install_only_service.py",
                    "--key",
                    args.key,
                ]
            )
        else:
            command.extend([args.image, "/main.py"])
        _run(command)

        if args.mode == "install-only":
            waited = subprocess.run(
                ["docker", "wait", container],
                check=True,
                text=True,
                capture_output=True,
                timeout=args.update_timeout,
            )
            exit_code = int(waited.stdout.strip())
            if exit_code != 0:
                raise RuntimeError(f"Install-only validator exited with {exit_code}")
            return {
                "service_key": args.key,
                "process_name": process_name,
                "previous_version": args.previous,
                "selector": args.selector,
                "validation_mode": "install_only",
                "result": "passed",
            }

        startup = _wait_for_startup(container, args.startup_timeout)
        service_state = (startup.get("services") or {}).get(process_name) or {}
        if startup.get("phase") != "ready" or service_state.get("state") != "ready":
            raise RuntimeError(
                f"Previous-version startup failed: phase={startup.get('phase')}, "
                f"service={service_state or 'missing'}"
            )
        _probe_service_urls(container, args.health_url)

        update_target = args.update_target or "latest"
        if args.target_version:
            _set_configured_release_target(container, process_name, args.target_version)
            update_target = args.update_target or "configured"
        update = _post_update(
            container,
            process_name,
            args.update_timeout,
            target=None if update_target == "channel" else update_target,
        )
        if update.get("status") not in {"updated", "no_update"}:
            raise RuntimeError(f"Update API did not succeed: {update}")

        encoded_name = urllib.parse.quote(process_name, safe="")
        health = _docker_exec_json(
            container,
            "/process/service-status?"
            f"process_name={encoded_name}&include_health=true",
        )
        if health.get("status") != "running" or health.get("healthy") is not True:
            raise RuntimeError(f"Updated service is not healthy: {health}")
        _probe_service_urls(container, args.health_url)

        return {
            "service_key": args.key,
            "process_name": process_name,
            "previous_version": args.previous,
            "target_version": args.target_version or "latest",
            "selector": args.selector,
            "startup_phase": startup.get("phase"),
            "update_status": update.get("status"),
            "update_message": update.get("message"),
            "health_status": health.get("health_status"),
            "health_urls": list(args.health_url),
            "result": "passed",
        }
    except Exception:
        print(_container_log_tail(container), file=sys.stderr)
        raise
    finally:
        _run(["docker", "stop", "-t", "30", container], check=False)
        _run(["docker", "rm", container], check=False)
        if args.keep:
            print(f"Retained regression state: {regression_root}", file=sys.stderr)
        else:
            _remove_regression_state(regression_root, args.image)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", required=True, help="DUMB service config key")
    parser.add_argument("--instance", help="Instance name for instance-based services")
    parser.add_argument("--previous", required=True, help="Older version to install")
    parser.add_argument(
        "--target-version",
        help="Persist this release after prior-version startup and install the configured target",
    )
    parser.add_argument(
        "--update-target",
        choices=("latest", "configured", "channel"),
        help="Update action after target configuration; channel keeps a moving release selector active",
    )
    parser.add_argument(
        "--selector",
        choices=("release", "pinned"),
        default="release",
    )
    parser.add_argument(
        "--mode",
        choices=("update", "install-only"),
        default="update",
    )
    parser.add_argument(
        "--enable-dependency",
        action="append",
        default=[],
        help="Additional non-instance service key to enable",
    )
    parser.add_argument(
        "--config-overrides-json",
        default="{}",
        help="JSON object merged only into the disposable regression config",
    )
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--cache-dir")
    parser.add_argument("--startup-timeout", type=int, default=600)
    parser.add_argument("--update-timeout", type=int, default=1800)
    parser.add_argument(
        "--health-url",
        action="append",
        default=[],
        help="Container-local service URL that must succeed before and after update",
    )
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    try:
        args.config_overrides = json.loads(args.config_overrides_json)
    except json.JSONDecodeError as exc:
        parser.error(f"invalid --config-overrides-json: {exc}")
    if not isinstance(args.config_overrides, dict):
        parser.error("--config-overrides-json must contain an object")
    return args


def main() -> int:
    try:
        report = run_regression(parse_args())
    except Exception as exc:
        print(f"Regression failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
