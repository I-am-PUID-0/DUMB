from utils import postgres
from utils.config_loader import CONFIG_MANAGER
from utils.global_logger import logger
from utils.download import Downloader
from utils.versions import Versions
from utils.plex import PlexInstaller
from utils.traefik_setup import setup_traefik
from utils.user_management import chown_recursive, chown_single
import xml.etree.ElementTree as ET
import os, shutil, random, subprocess, re, glob, secrets, shlex, time, urllib.parse, base64, threading

# import yaml, platform, tarfile, tempfile, urllib.request

user_id = CONFIG_MANAGER.get("puid")
group_id = CONFIG_MANAGER.get("pgid")
downloader = Downloader()
versions = Versions()
_INSTALL_LOCKS = {}


def _get_install_lock(key: str) -> threading.Lock:
    lock = _INSTALL_LOCKS.get(key)
    if lock is None:
        lock = threading.Lock()
        _INSTALL_LOCKS[key] = lock
    return lock


def _resolve_arr_install_dir(
    key: str, instance_name: str, instance: dict
) -> tuple[str, bool]:
    shared_dir = f"/opt/{key}"
    instance_slug = instance_name.lower().replace(" ", "_")
    instance_dir = os.path.join(shared_dir, "instances", instance_slug)

    # Use instance-specific directory when using GitHub-based installation
    # This prevents conflicts when instances use different repos/versions/branches
    use_github = (
        instance.get("repo_owner")
        and instance.get("repo_name")
        and (instance.get("release_version_enabled") or instance.get("branch_enabled"))
    )
    if use_github:
        return instance_dir, True

    # Use instance-specific directory when pinned_version differs from shared
    pinned_version = (instance.get("pinned_version") or "").strip()
    if not pinned_version:
        return shared_dir, False

    shared_version, _ = versions.read_arr_version_from_dir(key, shared_dir)
    if shared_version and shared_version == pinned_version:
        return shared_dir, False

    return instance_dir, True


def _chown_recursive_if_needed(path: str, user_id: int, group_id: int) -> None:
    try:
        stat_info = os.stat(path)
    except Exception as e:
        logger.debug("Failed stat for %s: %s", path, e)
        stat_info = None
    chown_single(path, user_id, group_id)
    if stat_info and stat_info.st_uid == user_id and stat_info.st_gid == group_id:
        logger.debug(
            "Skipping recursive chown for %s; owner matches %s:%s",
            path,
            user_id,
            group_id,
        )
        return
    ok, err = chown_recursive(path, user_id, group_id)
    if err:
        logger.debug("Recursive chown failed for %s: %s", path, err)


def _update_zilean_connection_string(
    conn: str,
    postgres_host: str,
    postgres_port: int,
    postgres_user: str,
    postgres_password: str,
) -> str:
    if not isinstance(conn, str) or not conn:
        return conn

    trailing_semicolon = conn.endswith(";")
    parts = []
    for segment in conn.split(";"):
        if not segment:
            continue
        if "=" in segment:
            key, value = segment.split("=", 1)
            parts.append([key, value])
        else:
            parts.append([segment, ""])

    values = {key.strip().lower(): value for key, value in parts}
    current_db = values.get("database")
    if current_db and current_db.lower() != "zilean":
        return conn

    updated = False
    for pair in parts:
        key = pair[0].strip().lower()
        if key == "host" and pair[1] != postgres_host:
            pair[1] = postgres_host
            updated = True
        elif key == "port" and pair[1] != str(postgres_port):
            pair[1] = str(postgres_port)
            updated = True
        elif key == "username" and pair[1] != postgres_user:
            pair[1] = postgres_user
            updated = True
        elif key == "password" and pair[1] != postgres_password:
            pair[1] = postgres_password
            updated = True

    if not updated:
        return conn

    rebuilt = ";".join(f"{k}={v}" if v != "" else k for k, v in parts)
    if trailing_semicolon:
        rebuilt += ";"
    return rebuilt


def _update_postgres_url(
    conn: str,
    postgres_host: str,
    postgres_port: int,
    postgres_user: str,
    postgres_password: str,
) -> str:
    if not isinstance(conn, str) or not conn:
        return conn

    try:
        parsed = urllib.parse.urlparse(conn)
    except Exception:
        return conn

    if parsed.scheme not in ("postgres", "postgresql", "postgresql+psycopg2"):
        return conn

    netloc = f"{postgres_user}:{postgres_password}@{postgres_host}:{postgres_port}"
    updated = parsed._replace(netloc=netloc)
    return urllib.parse.urlunparse(updated)


def setup_release_version(process_handler, config, process_name, key):
    if key == "plex_debrid":
        return False, "Release version not supported for plex_debrid."

    logger.info(f"Using release version {config['release_version']} for {process_name}")

    if key in [
        "bazarr",
        "sonarr",
        "radarr",
        "lidarr",
        "prowlarr",
        "readarr",
    ]:
        target_dir = os.path.join(f"/opt/{key.lower()}")
    else:
        target_dir = config["config_dir"]

    os.makedirs(target_dir, exist_ok=True)

    if config.get("clear_on_update"):
        exclude_dirs = config.get("exclude_dirs", [])
        success, error = clear_directory(target_dir, exclude_dirs)
        if not success:
            return False, f"Failed to clear directory: {error}"
    else:
        exclude_dirs = None

    success, error = downloader.download_release_version(
        process_name=process_name,
        key=key,
        repo_owner=config["repo_owner"],
        repo_name=config["repo_name"],
        release_version=config["release_version"],
        target_dir=target_dir,
        zip_folder_name=None,
        exclude_dirs=exclude_dirs,
    )
    if not success:
        return False, f"Failed to download release: {error}"

    if key == "zurg":
        downloader.set_permissions(config["command"], 0o755)
        if not os.path.exists(os.path.join(config["config_dir"], "logs")):
            os.makedirs(os.path.join(config["config_dir"], "logs"), exist_ok=True)
            chown_recursive(
                config["config_dir"],
                CONFIG_MANAGER.get("puid"),
                CONFIG_MANAGER.get("pgid"),
            )

    if key == "zilean":
        versions.version_write(
            process_name,
            key,
            version_path=os.path.join(config["config_dir"], "version.txt"),
            version=config["release_version"],
        )

    elif key == "decypharr":
        versions.version_write(
            process_name,
            key,
            version_path=os.path.join(config["config_dir"], "version.txt"),
            version=config["release_version"],
        )
    elif key == "nzbdav":
        versions.version_write(
            process_name,
            key,
            version_path=os.path.join(config["config_dir"], "version.txt"),
            version=config["release_version"],
        )
    elif key == "tautulli":
        versions.version_write(
            process_name,
            key,
            version_path=os.path.join(config["config_dir"], "version.txt"),
            version=config["release_version"],
        )
    elif key == "seerr":
        versions.version_write(
            process_name,
            key,
            version_path=os.path.join(config["config_dir"], "version.txt"),
            version=config["release_version"],
        )

    success, error = additional_setup(process_handler, process_name, config, key)
    if not success:
        return False, error

    return True, None


def setup_branch_version(process_handler, config, process_name, key):
    if key in [
        "bazarr",
        "sonarr",
        "radarr",
        "lidarr",
        "prowlarr",
        "readarr",
    ]:
        target_dir = os.path.join(f"/opt/{key.lower()}")
    else:
        target_dir = config["config_dir"]
    if key == "zurg":
        return False, "Branch version not supported for Zurg."
    else:
        logger.info(f"Using branch {config['branch']} for {process_name}")
        os.makedirs(target_dir, exist_ok=True)
        branch_url, zip_folder_name = downloader.get_branch(
            config["repo_owner"], config["repo_name"], config["branch"]
        )
        if not branch_url:
            return False, f"Failed to fetch branch {config['branch']}"

        exclude_dirs = None
        if config.get("clear_on_update"):
            exclude_dirs = config.get("exclude_dirs", [])
            success, error = clear_directory(target_dir, exclude_dirs)
            if not success:
                return False, f"Failed to clear directory: {error}"

        success, error = downloader.download_and_extract(
            branch_url,
            target_dir,
            zip_folder_name=zip_folder_name,
            exclude_dirs=exclude_dirs,
        )
        if not success:
            return False, f"Failed to download branch: {error}"

        success, error = additional_setup(process_handler, process_name, config, key)
        if not success:
            return False, error

    return True, None


def additional_setup(process_handler, process_name, config, key):
    if key == "riven_frontend":
        success, error = vite_modifications(config["config_dir"])
        if not success:
            return False, f"Failed to make vite modifications: {error}"

    if key == "nzbdav":
        success, error = setup_nzbdav_build(process_handler, config)
        if not success:
            return False, error

    if config.get("platforms") and key != "nzbdav":
        success, error = setup_environment(
            process_handler, key, config["platforms"], config["config_dir"]
        )
        if not success:
            return (
                False,
                f"Failed to set up environment for {process_name}: {error}",
            )

    if key == "plex_debrid":
        success, error = chown_recursive(
            os.path.join(config["config_dir"], "config"), user_id, group_id
        )
        if not success:
            return False, error

    if key == "cli_debrid":
        utilities_dir = os.path.join(config["config_dir"], "utilities")
        success, error = chown_recursive(utilities_dir, user_id, group_id)
        if not success:
            return False, error

    if key == "decypharr" and config.get("branch_enabled"):
        success, error = build_decypharr_dev(process_handler, config)
        if not success:
            return False, f"Failed to build Decypharr development environment: {error}"

    return True, None


def _needs_riven_bootstrap(key, config_dir):
    if not config_dir:
        return False
    if key == "riven_backend":
        return not os.path.isfile(os.path.join(config_dir, "pyproject.toml"))
    if key == "riven_frontend":
        return not os.path.isfile(os.path.join(config_dir, "package.json"))
    return False


def _maybe_patch_riven_plexapi_dependency(
    process_handler, key, config_dir, poetry_executable, env
):
    if key != "riven_backend":
        return True, None
    pyproject_path = os.path.join(config_dir, "pyproject.toml")
    if not os.path.isfile(pyproject_path):
        logger.warning("Riven pyproject.toml not found at %s", pyproject_path)
        return True, None

    with open(pyproject_path, "r") as file:
        lines = file.readlines()

    version_match = None
    for line in lines:
        if re.match(r'^\s*version\s*=\s*"', line):
            version_match = re.search(r'"\s*([^"]+)\s*"', line)
            break
    if not version_match:
        logger.debug(
            "Riven version not found in %s; skipping plexapi patch", pyproject_path
        )
        return True, None

    release_version = version_match.group(1).strip().lower()
    if release_version != "0.23.6":
        return True, None

    start_idx = None
    end_idx = None
    brace_balance = 0
    indent = ""
    for i, line in enumerate(lines):
        if start_idx is None and re.search(r"^\s*plexapi\s*=\s*\{", line):
            start_idx = i
            indent_match = re.match(r"^(\s*)", line)
            indent = indent_match.group(1) if indent_match else ""
        if start_idx is not None:
            brace_balance += line.count("{") - line.count("}")
            if brace_balance <= 0:
                end_idx = i
                break

    if start_idx is None:
        logger.info("Riven plexapi dependency not found in %s", pyproject_path)
        return True, None

    replacement_line = f'{indent}plexapi = "4.17.0"\n'
    if lines[start_idx : end_idx + 1] == [replacement_line]:
        return True, None

    lines[start_idx : end_idx + 1] = [replacement_line]
    with open(pyproject_path, "w") as file:
        file.writelines(lines)

    logger.info("Pinned Riven plexapi to 4.17.0 for release %s", release_version)
    process_handler.start_process(
        "poetry_update_plexapi",
        config_dir,
        [poetry_executable, "update", "plexapi"],
        env=env,
    )
    process_handler.wait("poetry_update_plexapi")
    if process_handler.returncode != 0:
        return False, f"Error updating plexapi dependency: {process_handler.stderr}"

    return True, None


def install_project(process_handler, process_name):
    return _setup_project(
        process_handler, process_name, install_phase=True, configure_phase=False
    )


def configure_project(process_handler, process_name):
    return _setup_project(
        process_handler, process_name, install_phase=False, configure_phase=True
    )


def setup_project(process_handler, process_name, preinstall: bool = False):
    if preinstall:
        return install_project(process_handler, process_name)
    success, error = install_project(process_handler, process_name)
    if not success:
        return False, error
    return configure_project(process_handler, process_name)


def _setup_project(
    process_handler,
    process_name,
    install_phase: bool,
    configure_phase: bool,
):
    key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
    if not key:
        raise ValueError(f"Key for {process_name} not found in the configuration.")

    config = CONFIG_MANAGER.get_instance(instance_name, key)
    if not config:
        raise ValueError(f"Configuration for {process_name} not found.")

    with process_handler.setup_tracker_lock:
        already_setup = process_name in process_handler.setup_tracker
    if configure_phase and already_setup and not key == "nzbdav":
        process_handler.logger.info(
            f"{process_name} is already set up. Skipping setup."
        )
        return True, None

    if install_phase and not configure_phase:
        logger.info(f"Installing {process_name} artifacts...")
    elif configure_phase and not install_phase:
        logger.info(f"Configuring {process_name}...")
    else:
        logger.info(f"Setting up {process_name}...")
    try:
        bootstrap_installed = False
        if install_phase:
            if (
                not config.get("release_version_enabled")
                and not config.get("branch_enabled")
                and _needs_riven_bootstrap(key, config.get("config_dir"))
            ):
                original_release_version = config.get("release_version")
                if (
                    not original_release_version
                    or original_release_version.lower() != "latest"
                ):
                    config["release_version"] = "latest"
                logger.info(
                    "Riven not found at %s; bootstrapping release %s",
                    config.get("config_dir"),
                    config.get("release_version"),
                )
                success, error = setup_release_version(
                    process_handler, config, process_name, key
                )
                if not success:
                    return False, error
                bootstrap_installed = True
                if original_release_version is not None:
                    config["release_version"] = original_release_version
                else:
                    config.pop("release_version", None)

            requested_version = config.get("release_version")
            requested_lower = (requested_version or "").lower()
            allow_release_with_auto_update = (
                requested_lower in {"latest", "prerelease"}
                or "nightly" in requested_lower
            )
            if (
                not bootstrap_installed
                and config.get("release_version_enabled")
                and (not config.get("auto_update") or allow_release_with_auto_update)
            ):
                repo_owner = config.get("repo_owner")
                repo_name = config.get("repo_name")
                if not requested_version:
                    logger.warning(
                        f"Release version enabled for {process_name} but no version provided."
                    )
                else:
                    is_latest = requested_lower == "latest"
                    nightly = "nightly" in requested_lower
                    prerelease = requested_lower == "prerelease"
                    if is_latest or nightly or prerelease:
                        update_needed, update_info = versions.compare_versions(
                            process_name,
                            repo_owner,
                            repo_name,
                            instance_name,
                            key,
                            nightly=nightly,
                            prerelease=prerelease,
                        )

                        if update_needed:
                            logger.info(
                                f"Update needed for {process_name}: {update_info['latest_version']}, but using the requested version: {requested_version}"
                            )
                            success, error = setup_release_version(
                                process_handler, config, process_name, key
                            )
                            if not success:
                                return False, error
                        else:
                            logger.info(
                                f"No update needed for {process_name}: current version is {update_info['current_version']}, and requested version is: {requested_version}"
                            )
                    else:
                        current_version, error = versions.version_check(
                            process_name, instance_name, key
                        )
                        if not current_version:
                            logger.warning(
                                "Failed to read current version for %s: %s",
                                process_name,
                                error,
                            )
                            current_version = "0.0.0"
                        if current_version != requested_version:
                            logger.info(
                                f"Installing requested version for {process_name}: {requested_version} (current: {current_version})"
                            )
                            success, error = setup_release_version(
                                process_handler, config, process_name, key
                            )
                            if not success:
                                return False, error
                        else:
                            logger.info(
                                f"No update needed for {process_name}: current version matches requested version {requested_version}"
                            )

            elif not bootstrap_installed and config.get("branch_enabled"):
                success, error = setup_branch_version(
                    process_handler, config, process_name, key
                )
                if not success:
                    return False, error

        if configure_phase and config.get("env_copy"):
            src, dest = config["env_copy"]["source"], config["env_copy"]["destination"]
            if os.path.exists(src):
                shutil.copy(src, dest)
                logger.info(f"Copied .env from {src} to {dest}")

        if configure_phase and key == "nzbdav":
            webdav_password = (config.get("webdav_password") or "").strip()
            if not webdav_password:
                webdav_password = secrets.token_urlsafe(24)
                config["webdav_password"] = webdav_password
                CONFIG_MANAGER.save_config(process_name)
                logger.info("Generated NzbDAV WebDAV password.")

            backend_port = str(config.get("backend_port", 8080))
            default_env = {
                "LOG_LEVEL": config.get("log_level", "INFO").upper(),
                "WEBDAV_PASSWORD": webdav_password,
                "ASPNETCORE_URLS": f"http://+:{backend_port}",
                "CONFIG_PATH": config.get("config_dir") or "/nzbdav",
                "PORT": str(config.get("frontend_port", 3000)),
                "NODE_ENV": "production",
                "BACKEND_URL": f"http://127.0.0.1:{backend_port}",
                "FRONTEND_BACKEND_API_KEY": secrets.token_hex(32),
            }
            env = default_env.copy()
            for env_key, value in (config.get("env") or {}).items():
                if env_key not in default_env:
                    env[env_key] = value
            version_path = os.path.join(
                config.get("config_dir", "/nzbdav"), "version.txt"
            )
            if os.path.exists(version_path):
                try:
                    with open(version_path, "r") as f:
                        env.setdefault("NZBDAV_VERSION", f.read().strip())
                except OSError:
                    logger.warning(
                        "Failed to read NzbDAV version file at %s", version_path
                    )
            config["env"] = env

        if configure_phase and config.get("env"):
            env_changed = False
            for env_key, value in config["env"].items():
                if not isinstance(value, str):
                    continue

                updated_value = value
                if key == "zilean" and env_key == "ASPNETCORE_URLS":
                    port = str(config.get("port", 8182))
                    if updated_value.startswith("http://+:"):
                        updated_value = f"http://+:{port}"
                if key == "cli_debrid" and env_key == "CLI_DEBRID_PORT":
                    updated_value = str(config.get("port", 5000))
                if key == "cli_battery" and env_key == "CLI_DEBRID_BATTERY_PORT":
                    updated_value = str(config.get("port", 5001))
                if key == "zilean" and env_key == "Zilean__Database__ConnectionString":
                    postgres_host = CONFIG_MANAGER.get("postgres").get(
                        "host", "127.0.0.1"
                    )
                    postgres_port = CONFIG_MANAGER.get("postgres").get("port", 5432)
                    postgres_user = CONFIG_MANAGER.get("postgres").get("user", "DUMB")
                    postgres_password = CONFIG_MANAGER.get("postgres").get(
                        "password", "postgres"
                    )
                    updated_value = _update_zilean_connection_string(
                        updated_value,
                        postgres_host,
                        postgres_port,
                        postgres_user,
                        postgres_password,
                    )
                elif key == "riven_backend" and env_key in (
                    "RIVEN_DATABASE_URL",
                    "RIVEN_DATABASE_HOST",
                ):
                    postgres_host = CONFIG_MANAGER.get("postgres").get(
                        "host", "127.0.0.1"
                    )
                    postgres_port = CONFIG_MANAGER.get("postgres").get("port", 5432)
                    postgres_user = CONFIG_MANAGER.get("postgres").get("user", "DUMB")
                    postgres_password = CONFIG_MANAGER.get("postgres").get(
                        "password", "postgres"
                    )
                    updated_value = _update_postgres_url(
                        updated_value,
                        postgres_host,
                        postgres_port,
                        postgres_user,
                        postgres_password,
                    )

                if (
                    "{" in updated_value
                    and "}" in updated_value
                    and "$" not in updated_value
                ):
                    if key == "zilean":
                        postgres_host = CONFIG_MANAGER.get("postgres").get(
                            "host", "127.0.0.1"
                        )
                        postgres_port = CONFIG_MANAGER.get("postgres").get("port", 5432)
                        postgres_user = CONFIG_MANAGER.get("postgres").get(
                            "user", "DUMB"
                        )
                        postgres_password = CONFIG_MANAGER.get("postgres").get(
                            "password", "postgres"
                        )
                        updated_value = (
                            updated_value.replace("{postgres_host}", postgres_host)
                            .replace("{postgres_port}", str(postgres_port))
                            .replace("{postgres_user}", postgres_user)
                            .replace("{postgres_password}", postgres_password)
                        )

                    for placeholder in config.keys():
                        placeholder_pattern = f"{{{placeholder}}}"
                        if placeholder_pattern in updated_value:
                            updated_value = updated_value.replace(
                                placeholder_pattern, str(config[placeholder])
                            )

                if config["env"].get(env_key) != updated_value:
                    env_changed = True
                    config["env"][env_key] = updated_value
            if env_changed:
                CONFIG_MANAGER.save_config(process_name)

        if configure_phase and key == "dumb_frontend":
            success, error = dumb_frontend_setup()
            if not success:
                return False, error
            CONFIG_MANAGER.save_config(process_name)

        if configure_phase and key == "riven_frontend":
            copy_server_config(
                "/config/server.json",
                os.path.join(config["config_dir"], "config/server.json"),
            )

            frontend_host = config.get("host") or "0.0.0.0"
            frontend_port = config.get("port") or 3000
            origin = config.get("origin") or "http://localhost:3000"

            logger.debug(
                f"Setting up Riven Frontend with host: {frontend_host}, port: {frontend_port}, origin: {origin}"
            )

            existing_env = config.get("env", {}).copy()
            existing_env.update(
                {
                    "ORIGIN": origin,
                    "HOST": frontend_host,
                    "PORT": str(frontend_port),
                }
            )
            config["env"] = existing_env
        if configure_phase and key == "riven_backend":
            port = str(config.get("port", 8080))
            command = config.get("command", [])
            if not isinstance(command, list):
                raise ValueError(f"Unexpected type for command: {type(command)}")

            for i, arg in enumerate(command):
                if arg in ("-p", "--port") and i + 1 < len(command):
                    if command[i + 1] != "{port}":
                        command[i + 1] = "{port}"
                    break
            else:
                command.extend(["-p", "{port}"])

            formatted_command = [
                arg.format(port=port) if "{port}" in arg else arg for arg in command
            ]

            config["command"] = formatted_command

            symlink_library_path = config.get("symlink_library_path")
            if symlink_library_path and not os.path.exists(symlink_library_path):
                os.makedirs(symlink_library_path, exist_ok=True)
                os.chown(symlink_library_path, user_id, group_id)

        if key == "zurg":
            success, error = zurg_setup(
                install_only=install_phase and not configure_phase,
                configure_only=configure_phase and not install_phase,
            )
            if not success:
                return False, error

        if configure_phase and key == "zilean":
            config_app_wwwroot_dir = os.path.join(
                config["config_dir"], "app", "wwwroot"
            )
            config_wwwroot_dir = os.path.join(config["config_dir"], "wwwroot")
            if not os.path.exists(config_wwwroot_dir):
                os.symlink(config_app_wwwroot_dir, config_wwwroot_dir)
            try:
                from utils.prowlarr_settings import ensure_custom_indexers

                prowlarr_cfg = CONFIG_MANAGER.get("prowlarr") or {}
                instances = (prowlarr_cfg.get("instances") or {}) or {}
                for _, inst in instances.items():
                    if not isinstance(inst, dict) or not inst.get("enabled"):
                        continue
                    config_dir = inst.get("config_dir")
                    if config_dir:
                        ensure_custom_indexers(config_dir, int(config.get("port", 8182)))
            except Exception as exc:
                logger.warning(
                    "Failed to sync Prowlarr custom indexers after Zilean update: %s",
                    exc,
                )

        if configure_phase and key == "rclone":
            success, error = rclone_setup()
            if not success:
                return False, error

        if configure_phase and key == "postgres":
            success, error = postgres.postgres_setup(process_handler)
            if not success:
                return False, error

        if key == "plex":
            success, error = setup_plex(
                install_only=install_phase and not configure_phase,
                configure_only=configure_phase and not install_phase,
            )
            if not success:
                return False, error

        if key == "tautulli":
            success, error = setup_tautulli(
                process_handler,
                install_only=install_phase and not configure_phase,
                configure_only=configure_phase and not install_phase,
            )
            if not success:
                return False, error

        if key == "huntarr":
            success, error = setup_huntarr(
                process_handler,
                install_only=install_phase and not configure_phase,
                configure_only=configure_phase and not install_phase,
            )
            if not success:
                return False, error
        if key == "seerr":
            success, error = setup_seerr(
                process_handler,
                install_only=install_phase and not configure_phase,
                configure_only=configure_phase and not install_phase,
            )
            if not success:
                return False, error

        if key == "jellyfin":
            success, error = setup_jellyfin(
                install_only=install_phase and not configure_phase,
                configure_only=configure_phase and not install_phase,
            )
            if not success:
                return False, error

        if key == "emby":
            success, error = setup_emby(
                install_only=install_phase and not configure_phase,
                configure_only=configure_phase and not install_phase,
            )
            if not success:
                return False, error

        if configure_phase and key == "pgadmin":
            success, error = postgres.pgadmin_setup(process_handler)
            if not success:
                return False, error

        if configure_phase and key == "plex_debrid":
            success, error = plex_debrid_setup()
            if not success:
                return False, error

        if key == "phalanx_db":
            success, error = phalanx_setup(
                process_handler,
                install_only=install_phase and not configure_phase,
                configure_only=configure_phase and not install_phase,
            )
            if not success:
                return False, error

        if key == "decypharr":
            success, error = setup_decypharr(
                install_only=install_phase and not configure_phase,
                configure_only=configure_phase and not install_phase,
            )
            if not success:
                return False, error

        if key == "nzbdav":
            success, error = setup_nzbdav(
                process_handler,
                install_only=install_phase and not configure_phase,
                configure_only=configure_phase and not install_phase,
            )
            if not success:
                return False, error

        if key == "cli_debrid":
            utilities_dir = os.path.join(
                config.get("config_dir", "/cli_debrid"), "utilities"
            )
            if os.path.isdir(utilities_dir):
                success, error = chown_recursive(utilities_dir, user_id, group_id)
                if not success:
                    return False, error

        if key == "bazarr":
            success, error = setup_bazarr(
                process_handler,
                install_only=install_phase and not configure_phase,
                configure_only=configure_phase and not install_phase,
            )
            if not success:
                return False, error

        if key == "traefik":
            success, error = setup_traefik(
                process_handler,
                install_only=install_phase and not configure_phase,
                configure_only=configure_phase and not install_phase,
            )
            if not success:
                return False, error

        if key in [
            "sonarr",
            "radarr",
            "lidarr",
            "prowlarr",
            "readarr",
            "whisparr",
            "whisparr-v3",
        ]:
            if install_phase and not configure_phase:
                success, error = install_arr_instances(key, process_handler=process_handler)
                if not success:
                    return False, error
            elif configure_phase:
                if instance_name:
                    instance = CONFIG_MANAGER.get_instance(instance_name, key)
                    if instance and instance.get("enabled"):
                        logger.debug(
                            "Setting up %s with instance name %s for instance %s...",
                            process_name,
                            instance_name,
                            instance,
                        )
                        success, error = setup_arr_instance(
                            key,
                            instance_name,
                            instance,
                            process_name,
                            configure_only=True,
                            process_handler=process_handler,
                        )
                        if not success:
                            return False, error
                else:
                    for instance_name, instance in (
                        CONFIG_MANAGER.get(key, {}).get("instances", {}).items()
                    ):
                        if not instance.get("enabled"):
                            continue
                        process_name = instance["process_name"]
                        logger.debug(
                            "Setting up %s with instance name %s for instance %s...",
                            process_name,
                            instance_name,
                            instance,
                        )
                        success, error = setup_arr_instance(
                            key,
                            instance_name,
                            instance,
                            process_name,
                            configure_only=True,
                            process_handler=process_handler,
                        )
                        if not success:
                            return False, error

        if configure_phase:
            with process_handler.setup_tracker_lock:
                process_handler.setup_tracker.add(process_name)
                tracker_snapshot = set(process_handler.setup_tracker)
            logger.debug(f"Post Setup tracker: {tracker_snapshot}")
            logger.info(f"{process_name} setup complete")
        elif install_phase:
            logger.info(f"{process_name} install phase complete")
        return True, None

    except Exception as e:
        return False, f"Error during setup of {process_name}: {e}"


def ensure_arr_config(
    app_name: str, config_file: str, port: int, loglevel: str
) -> None:
    import xml.etree.ElementTree as ET

    if not os.path.exists(config_file):
        os.makedirs(os.path.dirname(config_file), exist_ok=True)

        config_content = f"""<?xml version="1.0" encoding="utf-8"?>
<Config>
  <Port>{port}</Port>
  <LogLevel>{loglevel}</LogLevel>
  <InstanceName>{app_name}</InstanceName>
</Config>
"""
        with open(config_file, "w") as f:
            f.write(config_content)
        chown_single(config_file, user_id, group_id)
        _chown_recursive_if_needed(os.path.dirname(config_file), user_id, group_id)
        logger.info(f"[{app_name}] Created new config.xml at {config_file}")
        return

    try:
        tree = ET.parse(config_file)
        root = tree.getroot()
        port_elem = root.find("Port")
        loglevel_elem = root.find("LogLevel")
        Instance_elem = root.find("InstanceName")

        if port_elem is not None and int(port_elem.text) != port:
            logger.info(
                f"[{app_name}] Updating port in config.xml from {port_elem.text} to {port}"
            )
            port_elem.text = str(port)
            tree.write(config_file)
            chown_single(config_file, user_id, group_id)
        else:
            logger.debug(f"[{app_name}] Port already set to {port} in config.xml")
        if loglevel_elem is not None and loglevel_elem.text != loglevel:
            logger.info(
                f"[{app_name}] Updating log level in config.xml from {loglevel_elem.text} to {loglevel}"
            )
            loglevel_elem.text = loglevel
            tree.write(config_file)
            chown_single(config_file, user_id, group_id)
        else:
            logger.debug(
                f"[{app_name}] Log level already set to {loglevel} in config.xml"
            )
        if Instance_elem is not None and Instance_elem.text != app_name:
            logger.info(
                f"[{app_name}] Updating InstanceName in config.xml from {Instance_elem.text} to {app_name}"
            )
            Instance_elem.text = app_name
            tree.write(config_file)
            chown_single(config_file, user_id, group_id)
        chown_single(config_file, user_id, group_id)
    except Exception as e:
        logger.error(f"[{app_name}] Failed to update existing config.xml: {e}")


def _build_arr_from_source(process_handler, key, source_dir, binary_path):
    """
    Build an arr service from source code using dotnet.

    Args:
        process_handler: Process handler for running dotnet commands
        key: Service key (sonarr, radarr, etc.)
        source_dir: Directory containing the source code
        binary_path: Expected path to the final binary

    Returns:
        tuple: (success: bool, error: str or None)
    """
    try:
        import glob as glob_module
        import json

        app_name = key.capitalize()
        logger.info(f"Building {app_name} from source in {source_dir}...")

        # Step 1: Handle global.json - remove SDK version pinning to allow any installed SDK
        global_json_path = os.path.join(source_dir, "global.json")
        if os.path.exists(global_json_path):
            try:
                with open(global_json_path, "r") as f:
                    global_config = json.load(f)

                # Check if there's an SDK version requirement
                sdk_version = global_config.get("sdk", {}).get("version")
                if sdk_version:
                    logger.info(
                        f"{app_name} requires SDK {sdk_version}, modifying global.json to allow rollForward..."
                    )
                    # Modify to allow rolling forward to newer SDKs
                    global_config["sdk"]["rollForward"] = "latestMajor"
                    global_config["sdk"]["allowPrerelease"] = True
                    with open(global_json_path, "w") as f:
                        json.dump(global_config, f, indent=2)
            except Exception as e:
                logger.warning(f"Failed to modify global.json: {e}")
                # Try removing it entirely as fallback
                try:
                    os.rename(global_json_path, global_json_path + ".bak")
                    logger.info("Renamed global.json to global.json.bak")
                except Exception:
                    pass

        # Step 2: Find the solution file
        # Strip any git metadata to avoid Microsoft.Build.Tasks.Git repo format errors.
        removed_git = False
        for root, dirs, files in os.walk(source_dir):
            if ".git" in dirs:
                git_path = os.path.join(root, ".git")
                try:
                    shutil.rmtree(git_path)
                    removed_git = True
                except Exception as e:
                    logger.warning("Failed to remove %s: %s", git_path, e)
                dirs.remove(".git")
            if ".git" in files:
                git_file = os.path.join(root, ".git")
                try:
                    os.remove(git_file)
                    removed_git = True
                except Exception as e:
                    logger.warning("Failed to remove %s: %s", git_file, e)
        if removed_git:
            logger.info("Removed git metadata from source to avoid build metadata errors.")

        sln_patterns = [
            os.path.join(source_dir, "src", f"{app_name}.sln"),
            os.path.join(source_dir, "src", "*.sln"),
            os.path.join(source_dir, f"{app_name}.sln"),
            os.path.join(source_dir, "*.sln"),
        ]

        sln_file = None
        for pattern in sln_patterns:
            matches = glob_module.glob(pattern)
            if matches:
                sln_file = matches[0]
                break

        if not sln_file:
            return False, f"Could not find solution file (.sln) in {source_dir}"

        logger.info(f"Found solution file: {sln_file}")
        sln_dir = os.path.dirname(sln_file)

        # Step 3: Determine the main host project
        # Arr services typically have their main project in src/NzbDrone.Host or src/{App}.Host
        host_project_patterns = [
            os.path.join(sln_dir, "NzbDrone.Host", "*.csproj"),
            os.path.join(sln_dir, f"{app_name}.Host", "*.csproj"),
            os.path.join(sln_dir, "Servarr.Host", "*.csproj"),
        ]

        host_project = None
        for pattern in host_project_patterns:
            matches = glob_module.glob(pattern)
            if matches:
                host_project = matches[0]
                break

        # Step 4: Set up environment
        env = os.environ.copy()

        # Use /tmp for dotnet CLI home to avoid permission issues
        dotnet_home = os.path.join("/tmp", f"dotnet-{key}")
        os.makedirs(dotnet_home, exist_ok=True)

        # Pre-create dotnet CLI cache dirs to avoid first-run permission errors
        dotnet_cli_dir = os.path.join(dotnet_home, ".dotnet")
        os.makedirs(dotnet_cli_dir, exist_ok=True)

        nuget_packages = os.path.join(dotnet_home, "nuget", "packages")
        os.makedirs(nuget_packages, exist_ok=True)

        # Ensure runtime user can write to dotnet caches
        if user_id is not None and group_id is not None:
            try:
                os.chmod(dotnet_home, 0o775)
                os.chmod(dotnet_cli_dir, 0o775)
                os.chmod(os.path.dirname(nuget_packages), 0o775)
            except Exception as e:
                logger.debug("Failed to chmod dotnet cache dirs: %s", e)
            _chown_recursive_if_needed(dotnet_home, user_id, group_id)

        env["DOTNET_CLI_HOME"] = dotnet_home
        env["NUGET_PACKAGES"] = nuget_packages
        env["HOME"] = dotnet_home  # Some dotnet tools check HOME
        env["DOTNET_NOLOGO"] = "1"
        env["DOTNET_SKIP_FIRST_TIME_EXPERIENCE"] = "1"
        # Avoid audit warnings failing restore on branches
        env["NUGET_DISABLE_AUDIT"] = "1"

        # Ensure writable temp/cache locations
        temp_dir = os.path.join(source_dir, "_temp")
        os.makedirs(temp_dir, exist_ok=True)
        if user_id is not None and group_id is not None:
            try:
                os.chmod(temp_dir, 0o775)
            except Exception as e:
                logger.debug("Failed to chmod %s: %s", temp_dir, e)
            _chown_recursive_if_needed(temp_dir, user_id, group_id)

        tmp_root = os.path.join(dotnet_home, "tmp")
        os.makedirs(tmp_root, exist_ok=True)
        env["TMPDIR"] = tmp_root
        env["TEMP"] = tmp_root
        env["TMP"] = tmp_root

        # Step 5: Run dotnet restore
        logger.info(f"Running dotnet restore on {sln_file}...")
        process_handler.start_process(
            "dotnet_arr_restore",
            sln_dir,
            [
                "dotnet",
                "restore",
                sln_file,
                "/nodeReuse:false",
                "/p:TreatWarningsAsErrors=false",
            ],
            env=env,
        )
        process_handler.wait("dotnet_arr_restore")
        if process_handler.returncode != 0:
            return False, f"dotnet restore failed for {app_name}"

        # Step 6: Run dotnet publish
        output_dir = os.path.join(source_dir, "_output", app_name)
        os.makedirs(output_dir, exist_ok=True)

        publish_target = host_project or sln_file
        logger.info(f"Running dotnet publish on {publish_target}...")

        # Try to detect target framework from project to avoid NETSDK1129
        target_framework = None
        if publish_target and publish_target.endswith(".csproj"):
            try:
                with open(publish_target, "r") as f:
                    project_text = f.read()
                tf_match = re.search(r"<TargetFramework>([^<]+)</TargetFramework>", project_text)
                tfs_match = re.search(r"<TargetFrameworks>([^<]+)</TargetFrameworks>", project_text)
                if tf_match:
                    target_framework = tf_match.group(1).strip()
                elif tfs_match:
                    target_framework = tfs_match.group(1).split(";")[0].strip()
            except Exception as e:
                logger.debug("Failed to detect TargetFramework from %s: %s", publish_target, e)

        publish_cmd = [
            "dotnet", "publish", publish_target,
            "-c", "Release",
            "--no-restore",
            "-o", output_dir,
            "/nodeReuse:false",
            "/p:UseSharedCompilation=false",
            "/p:UseSourceLink=false",
            "/p:SourceLinkCreate=false",
            "/p:EnableSourceLink=false",
            "/p:RepositoryType=none",
            "/p:RepositoryUrl=",
            "/p:RepositoryCommit=",
            "/p:SourceRevisionId=unknown",
        ]
        if target_framework:
            publish_cmd.extend(["-f", target_framework])

        process_handler.start_process(
            "dotnet_arr_publish",
            sln_dir,
            publish_cmd,
            env=env,
        )
        process_handler.wait("dotnet_arr_publish")
        if process_handler.returncode != 0:
            return False, f"dotnet publish failed for {app_name}"

        # Step 7: Move built files to expected location
        expected_bin_dir = os.path.dirname(binary_path)
        if output_dir != expected_bin_dir:
            os.makedirs(expected_bin_dir, exist_ok=True)
            # Copy all files from output to expected location
            import shutil
            for item in os.listdir(output_dir):
                src = os.path.join(output_dir, item)
                dst = os.path.join(expected_bin_dir, item)
                if os.path.isdir(src):
                    if os.path.exists(dst):
                        shutil.rmtree(dst)
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            logger.info(f"Copied built files to {expected_bin_dir}")

        # Step 8: Verify binary exists
        if not os.path.exists(binary_path):
            # Try to find the binary with different name patterns
            possible_names = [
                os.path.join(expected_bin_dir, app_name),
                os.path.join(expected_bin_dir, f"{app_name}.dll"),
                os.path.join(expected_bin_dir, "NzbDrone"),
                os.path.join(expected_bin_dir, "NzbDrone.dll"),
            ]
            found = False
            for name in possible_names:
                if os.path.exists(name):
                    if name != binary_path and name.endswith(app_name):
                        # Create symlink or copy
                        shutil.copy2(name, binary_path)
                    found = True
                    break

            if not found:
                return False, f"Build completed but binary not found at {binary_path}"

        logger.info(f"{app_name} built successfully from source!")
        return True, None

    except Exception as e:
        return False, f"Error building {key} from source: {e}"


def _find_arr_binary(install_dir: str, key: str) -> str | None:
    app_name = key.capitalize()
    candidates = [
        os.path.join(install_dir, app_name, app_name),
        os.path.join(install_dir, app_name, f"{app_name}.dll"),
        os.path.join(install_dir, key, key),
        os.path.join(install_dir, key, app_name),
        os.path.join(install_dir, key, f"{key}.dll"),
        os.path.join(install_dir, key, f"{app_name}.dll"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    for root, _, files in os.walk(install_dir):
        for name in files:
            if name in {app_name, key, f"{app_name}.dll", f"{key}.dll"}:
                return os.path.join(root, name)
    return None


def _binary_interpreter_exists(binary_path: str) -> bool:
    try:
        result = subprocess.run(
            ["readelf", "-l", binary_path], capture_output=True, text=True
        )
        if result.returncode != 0:
            return True
        for line in result.stdout.splitlines():
            if "Requesting program interpreter" in line:
                interpreter = line.split(":", 1)[-1].strip().strip("[]")
                return os.path.exists(interpreter)
    except Exception:
        return True
    return True


def _install_arr_binary(key, instance_name, instance, process_name, process_handler=None):
    install_dir, is_instance_dir = _resolve_arr_install_dir(
        key, instance_name, instance
    )
    binary_path = os.path.join(install_dir, key.capitalize(), f"{key.capitalize()}")
    pinned_version = (instance.get("pinned_version") or "").strip()

    release_enabled = instance.get("release_version_enabled")
    branch_enabled = instance.get("branch_enabled")
    repo_owner = instance.get("repo_owner")
    repo_name = instance.get("repo_name")
    has_repo = repo_owner and repo_name

    # Arr branch builds require source compilation; disable branch installs for arr services
    if branch_enabled and key in {
        "sonarr",
        "radarr",
        "lidarr",
        "prowlarr",
        "readarr",
        "whisparr",
        "whisparr-v3",
    }:
        logger.warning(
            "%s has 'branch_enabled' set, but branch builds are disabled for arr services. "
            "Set 'release_version_enabled' instead.",
            process_name,
        )
        branch_enabled = False

    # Check for conflicting flags - release_version_enabled takes priority
    if release_enabled and branch_enabled:
        logger.warning(
            "%s has both 'release_version_enabled' and 'branch_enabled' set to True. "
            "Using 'release_version_enabled'. Set only one option to avoid this warning.",
            process_name,
        )
        branch_enabled = False

    # Determine if using a custom fork (non-official repo)
    official_repos = {
        "sonarr": ("Sonarr", "Sonarr"),
        "radarr": ("Radarr", "Radarr"),
        "lidarr": ("Lidarr", "Lidarr"),
        "prowlarr": ("Prowlarr", "Prowlarr"),
        "readarr": ("Readarr", "Readarr"),
        "whisparr": ("Whisparr", "Whisparr"),
        "whisparr-v3": ("Whisparr", "Whisparr"),
    }
    official_owner, official_name = official_repos.get(key, (None, None))
    is_custom_fork = has_repo and (repo_owner != official_owner or repo_name != official_name)

    use_github_release = release_enabled and has_repo
    # For branch_enabled, download source and build with dotnet
    use_github_branch_build = branch_enabled and has_repo

    with _get_install_lock(key):
        if use_github_branch_build:
            # Download source from GitHub branch and build with dotnet
            branch = instance.get("branch")
            logger.info(
                "Installing %s from GitHub branch (%s/%s @ %s) - building from source...",
                key.capitalize(),
                repo_owner,
                repo_name,
                branch,
            )
            from utils.download import Downloader
            downloader = Downloader()

            # Create install_dir if needed
            os.makedirs(install_dir, exist_ok=True)

            # Download branch source
            branch_url, zip_folder_name = downloader.get_branch(repo_owner, repo_name, branch)
            if not branch_url:
                return False, f"Failed to get branch URL for {branch}", install_dir, binary_path, is_instance_dir

            success, error = downloader.download_and_extract(
                branch_url,
                install_dir,
                zip_folder_name=zip_folder_name,
                headers=downloader.get_headers(),
                exclude_dirs=instance.get("exclude_dirs", []),
            )
            if not success:
                return False, error, install_dir, binary_path, is_instance_dir

            # Build from source using dotnet
            if process_handler:
                success, error = _build_arr_from_source(
                    process_handler, key, install_dir, binary_path
                )
                if not success:
                    return False, error, install_dir, binary_path, is_instance_dir
            else:
                logger.warning(
                    "%s source downloaded but cannot build without process_handler. "
                    "Service may not start.",
                    key.capitalize(),
                )

        elif use_github_release:
            # GitHub release-based installation (like other repo-based services)
            release_version = instance.get("release_version", "latest")
            current_version = None
            if os.path.exists(binary_path):
                current_version, _ = versions.read_arr_version_from_dir(key, install_dir)

            # Check if we need to install/update
            need_install = not os.path.exists(binary_path)
            if not need_install and os.path.exists(binary_path):
                if not _binary_interpreter_exists(binary_path):
                    logger.warning(
                        "%s binary interpreter missing for %s; forcing reinstall.",
                        key.capitalize(),
                        binary_path,
                    )
                    need_install = True
            if not need_install and current_version:
                # Compare versions to see if update needed
                from utils.download import Downloader
                downloader = Downloader()
                from utils.versions import Versions

                nightly = "nightly" in release_version.lower() if release_version else False
                prerelease = "prerelease" in release_version.lower() if release_version else False

                if release_version.lower() in ("latest", "nightly", "prerelease"):
                    latest_version, _ = downloader.get_latest_release(
                        instance.get("repo_owner"),
                        instance.get("repo_name"),
                        nightly=nightly,
                        prerelease=prerelease,
                    )
                    normalized_current = Versions._normalize_arr_version(current_version)
                    normalized_latest = Versions._normalize_arr_version(latest_version)
                    if latest_version and normalized_current != normalized_latest:
                        need_install = True
                        release_version = latest_version
                else:
                    normalized_current = Versions._normalize_arr_version(current_version)
                    normalized_target = Versions._normalize_arr_version(release_version)
                    if normalized_current != normalized_target:
                        need_install = True

            if need_install:
                logger.info(
                    "Installing %s from GitHub (%s/%s) version %s...",
                    key.capitalize(),
                    instance.get("repo_owner"),
                    instance.get("repo_name"),
                    release_version,
                )
                from utils.download import Downloader
                downloader = Downloader()

                # Create install_dir if needed
                os.makedirs(install_dir, exist_ok=True)

                success, error = downloader.download_release_version(
                    process_name=process_name,
                    key=key,
                    repo_owner=instance.get("repo_owner"),
                    repo_name=instance.get("repo_name"),
                    release_version=release_version,
                    target_dir=install_dir,
                )
                if not success:
                    return False, error, install_dir, binary_path, is_instance_dir
        else:
            # Traditional Arr updater installation (uses arr update servers)
            # This handles: default installs, pinned_version, and branch_enabled with official repos
            from utils.arr import ArrInstaller

            branch = instance.get("branch")
            need_install = not os.path.exists(binary_path)
            if not need_install and os.path.exists(binary_path):
                if not _binary_interpreter_exists(binary_path):
                    logger.warning(
                        "%s binary interpreter missing for %s; forcing reinstall.",
                        key.capitalize(),
                        binary_path,
                    )
                    need_install = True

            if need_install:
                if branch_enabled:
                    logger.info(
                        "Installing %s from arr update servers (branch: %s)...",
                        key.capitalize(),
                        branch,
                    )
                else:
                    logger.warning(
                        "%s binary not found in %s. Installing...",
                        key.capitalize(),
                        install_dir,
                    )

                installer = ArrInstaller(
                    key,
                    version=pinned_version or "4",
                    install_dir=install_dir,
                    branch=branch,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                )
                success, error = installer.install()
                if not success:
                    return False, error, install_dir, binary_path, is_instance_dir
            elif branch_enabled:
                # branch_enabled: check if we need to update to match the specified branch
                logger.info(
                    "Checking %s for branch update (branch: %s)...",
                    key.capitalize(),
                    branch,
                )
                installer = ArrInstaller(
                    key,
                    version=pinned_version or "4",
                    install_dir=install_dir,
                    branch=branch,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                )
                latest_version, _ = installer.get_latest_version()
                current_version, _ = versions.read_arr_version_from_dir(key, install_dir)
                if latest_version and current_version != latest_version:
                    logger.info(
                        "%s updating from %s to %s (branch: %s)...",
                        key.capitalize(),
                        current_version or "unknown",
                        latest_version,
                        branch,
                    )
                    success, error = installer.install()
                    if not success:
                        return False, error, install_dir, binary_path, is_instance_dir
            elif pinned_version:
                current_version, _ = versions.read_arr_version_from_dir(key, install_dir)
                if not current_version:
                    logger.warning(
                        "Failed to read %s version for pin check in %s.",
                        key.capitalize(),
                        install_dir,
                    )
                elif current_version != pinned_version:
                    logger.info(
                        "%s pinned to %s; installing over %s in %s.",
                        key.capitalize(),
                        pinned_version,
                        current_version,
                        install_dir,
                    )
                    from utils.arr import ArrInstaller

                    installer = ArrInstaller(
                        key,
                        version=pinned_version or "4",
                        install_dir=install_dir,
                        branch=instance.get("branch"),
                        repo_owner=instance.get("repo_owner"),
                        repo_name=instance.get("repo_name"),
                    )
                    success, error = installer.install()
                    if not success:
                        return False, error, install_dir, binary_path, is_instance_dir

        if not os.path.exists(binary_path):
            resolved = _find_arr_binary(install_dir, key)
            if resolved:
                binary_path = resolved
                logger.info("Resolved %s binary to %s", key.capitalize(), binary_path)
            else:
                return False, f"{key.capitalize()} binary missing in {install_dir}", install_dir, binary_path, is_instance_dir

        if not os.access(binary_path, os.X_OK):
            logger.warning("%s not executable. Fixing permissions...", binary_path)
            os.chmod(binary_path, 0o755)

        # Ensure bundled ffprobe is executable if present (Whisparr/others)
        ffprobe_path = os.path.join(os.path.dirname(binary_path), "ffprobe")
        if os.path.isfile(ffprobe_path) and not os.access(ffprobe_path, os.X_OK):
            logger.warning("ffprobe not executable at %s. Fixing permissions...", ffprobe_path)
            os.chmod(ffprobe_path, 0o755)
    return True, None, install_dir, binary_path, is_instance_dir


def setup_arr_instance(
    key, instance_name, instance, process_name, install_only=False, configure_only=False, process_handler=None
):
    if install_only and configure_only:
        return False, "Invalid arr setup phase."
    if configure_only:
        install_dir, _ = _resolve_arr_install_dir(key, instance_name, instance)
        binary_path = os.path.join(install_dir, key.capitalize(), f"{key.capitalize()}")
        if not os.path.exists(binary_path):
            resolved = _find_arr_binary(install_dir, key)
            if resolved:
                binary_path = resolved
                logger.info("Resolved %s binary to %s", key.capitalize(), binary_path)
            else:
                return False, f"{key.capitalize()} binary missing in {install_dir}."
    else:
        success, error, install_dir, binary_path, _ = _install_arr_binary(
            key, instance_name, instance, process_name, process_handler=process_handler
        )
        if not success:
            return False, error

    if not install_only and instance.get("install_dir") != install_dir:
        instance["install_dir"] = install_dir
        CONFIG_MANAGER.save_config(process_name)
    config_dir = instance["config_dir"]
    os.makedirs(config_dir, exist_ok=True)
    if configure_only:
        chown_single(config_dir, CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid"))
    else:
        _chown_recursive_if_needed(
            config_dir, CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
        )

    if install_only:
        return True, None

    logger.info(f"Setting up {process_name} environment...")
    config_file = instance["config_file"]
    port = instance.get("port", 8989)
    loglevel = instance.get("log_level", "INFO").upper()
    ensure_arr_config(process_name, config_file, port, loglevel)
    instance["command"] = [
        binary_path,
        "--nobrowser",
        f"--data={config_dir}",
    ]
    if key == "prowlarr":
        try:
            from utils.prowlarr_settings import ensure_custom_indexers

            zilean_cfg = CONFIG_MANAGER.get("zilean", {}) or {}
            zilean_port = int(zilean_cfg.get("port", 8182))
            ensure_custom_indexers(config_dir, zilean_port)
        except Exception as exc:
            logger.warning("Prowlarr custom indexer sync skipped: %s", exc)

    return True, None


def install_arr_instances(key, process_handler=None):
    instances = CONFIG_MANAGER.get(key, {}).get("instances", {}) or {}
    for instance_name, instance in instances.items():
        if not instance.get("enabled"):
            continue
        process_name = instance.get("process_name") or instance_name
        success, error = setup_arr_instance(
            key,
            instance_name,
            instance,
            process_name,
            install_only=True,
            process_handler=process_handler,
        )
        if not success:
            return False, error
    return True, None


def setup_bazarr(
    process_handler=None, install_only: bool = False, configure_only: bool = False
):
    config = CONFIG_MANAGER.get("bazarr", {})
    if not config:
        return False, "Bazarr configuration not found."
    try:
        if install_only and configure_only:
            return False, "Invalid Bazarr setup phase."

        def setup_bazarr_instance(instance_name, instance):
            if not instance.get("enabled", False):
                logger.debug(f"Skipping disabled Bazarr instance: {instance_name}")
                return True, None
            instance_config_dir = instance.get("config_dir") or (
                f"/bazarr/{instance_name.lower()}/config"
            )
            instance_config_file = instance.get("config_file") or (
                os.path.join(instance_config_dir, "config.yaml")
            )
            install_path = f"/opt/bazarr/{instance_name.lower()}"
            if not os.path.exists(instance_config_dir):
                logger.debug(
                    f"Creating Bazarr instance {instance_name} config directory: {instance_config_dir}"
                )
                os.makedirs(instance_config_dir, exist_ok=True)
                chown_recursive(instance_config_dir, user_id, group_id)
            ### check if the bazarr.py file exists in the install_path
            bazarr_py_path = os.path.join(install_path, "bazarr.py")
            needs_download = not os.path.exists(bazarr_py_path)
            if needs_download and configure_only:
                return False, f"Bazarr instance {instance_name} not installed."
            if needs_download:
                logger.warning(
                    f"Bazarr instance {instance_name} not found at {install_path}. Downloading..."
                )
                os.makedirs(install_path, exist_ok=True)
                chown_recursive(install_path, user_id, group_id)
                if not instance.get("branch_enabled"):
                    release, error = downloader.get_latest_release(
                        repo_owner=instance.get("repo_owner"),
                        repo_name=instance.get("repo_name"),
                    )
                    if not release:
                        return False, f"Failed to get latest release: {error}"

                    success, error = downloader.download_release_version(
                        process_name=instance.get("process_name"),
                        key="bazarr",
                        repo_owner=instance.get("repo_owner"),
                        repo_name=instance.get("repo_name"),
                        release_version=release,
                        target_dir=install_path,
                    )
                    if not success:
                        return False, f"Failed to download Bazarr: {error}"
                    platforms = instance.get("platforms", [])
                    success, error = setup_environment(
                        process_handler, "bazarr", platforms, install_path
                    )
                    if not success:
                        return (
                            False,
                            f"Failed to set up environment for Bazarr instance {instance_name}: {error}",
                        )
                    success, error = setup_pnpm_environment(
                        process_handler, f"{install_path}/frontend"
                    )
                    if not success:
                        return (
                            False,
                            f"Failed to set up pnpm environment for Bazarr instance {instance_name}: {error}",
                        )
                    chown_recursive(install_path, user_id, group_id)

            if install_only:
                return True, None

            instance["command"] = [
                f"{install_path}/venv/bin/python",
                f"{bazarr_py_path}",
                f"-c {instance_config_dir}",
                f"-p {instance.get('port')}",
            ]
            instance["env"] = {"NO_UPDATE": "true"}
            logger.info(f"Bazarr instance '{instance_name}' setup complete.")
            return True, None

        for instance_name, instance in config.get("instances", {}).items():
            if instance.get("enabled"):
                logger.info(f"Setting up enabled Bazarr instance: {instance_name}")
                success, error = setup_bazarr_instance(instance_name, instance)
                if not success:
                    return False, error
        logger.info("All Bazarr instances set up successfully.")
        return True, None
    except Exception as e:
        return False, f"Error during Bazarr setup: {e}"


def setup_decypharr(install_only: bool = False, configure_only: bool = False):
    config = CONFIG_MANAGER.get("decypharr")
    if not config:
        return False, "Configuration for Decypharr not found."

    logger.info("Starting Decypharr setup...")

    try:
        def _collect_decypharr_mounts(config_path: str) -> list[str]:
            if not config_path or not os.path.exists(config_path):
                return []
            try:
                import json

                with open(config_path, "r") as handle:
                    data = json.load(handle)
            except Exception as e:
                logger.debug("Failed to read Decypharr config for mounts: %s", e)
                return []

            rclone_cfg = data.get("rclone") or {}
            mount_base = rclone_cfg.get("mount_path") or "/mnt/debrid/decypharr"
            if not isinstance(mount_base, str) or not mount_base.strip():
                mount_base = "/mnt/debrid/decypharr"

            mounts = set()
            for debrid in data.get("debrids") or []:
                if not isinstance(debrid, dict):
                    continue
                name = debrid.get("name")
                if not name:
                    continue
                mounts.add(os.path.join(mount_base, str(name)))
            return sorted(mounts)

        def _unmount_decypharr_mounts(config_path: str) -> tuple[bool, str | None]:
            mount_paths = _collect_decypharr_mounts(config_path)
            if not mount_paths:
                return True, None
            for mount_path in mount_paths:
                if not is_mount_point(mount_path):
                    continue
                logger.info(
                    "Unmounting Decypharr rclone mount at %s before startup...",
                    mount_path,
                )
                umount = subprocess.run(
                    ["umount", mount_path], capture_output=True, text=True
                )
                if umount.returncode == 0:
                    logger.info("Successfully unmounted %s.", mount_path)
                else:
                    error_msg = umount.stderr.strip() or "unknown error"
                    logger.error(
                        "Failed to unmount %s: %s", mount_path, error_msg
                    )
            return True, None

        if install_only and configure_only:
            return False, "Invalid Decypharr setup phase."
        decypharr_config_dir = config.get("config_dir")
        decypharr_config_file = config.get("config_file")
        decypharr_binary_file = "decypharr"
        binary_path = os.path.join(decypharr_config_dir, decypharr_binary_file)
        decypharr_embedded_rclone = config.get("use_embedded_rclone", False)
        if not os.path.exists(decypharr_config_dir):
            logger.debug(
                f"Creating Decypharr config directory at {decypharr_config_dir}"
            )
            os.makedirs(decypharr_config_dir, exist_ok=True)
        chown_single(decypharr_config_dir, user_id, group_id)

        if decypharr_embedded_rclone and decypharr_config_file:
            _unmount_decypharr_mounts(decypharr_config_file)

        if not configure_only and not os.path.isfile(binary_path):
            logger.warning(
                f"Decypharr project not found at {decypharr_config_dir}. Downloading..."
            )
            if not config.get("branch_enabled"):
                release, error = downloader.get_latest_release(
                    repo_owner=config.get("repo_owner"),
                    repo_name=config.get("repo_name"),
                )
                if not release:
                    return False, f"Failed to get latest release: {error}"

                success, error = downloader.download_release_version(
                    process_name=config.get("process_name"),
                    key="decypharr",
                    repo_owner=config.get("repo_owner"),
                    repo_name=config.get("repo_name"),
                    release_version=release,
                    target_dir=decypharr_config_dir,
                )
                if not success:
                    return False, f"Failed to download Decypharr: {error}"

                versions.version_write(
                    process_name=config.get("process_name"),
                    key="decypharr",
                    version_path=os.path.join(decypharr_config_dir, "version.txt"),
                    version=release,
                )
        if os.path.isfile(binary_path):
            os.chmod(binary_path, 0o755)
            logger.debug(f"Marked {binary_path} as executable")
        elif configure_only:
            return False, f"Decypharr binary missing at {binary_path}."
        if decypharr_embedded_rclone:
            success, error = fuse_config()
            if not success:
                return False, error
        if install_only:
            logger.info("Decypharr install phase: skipping runtime config patch.")
        elif configure_only and os.path.exists(decypharr_config_file):
            from utils.decypharr_settings import patch_decypharr_config

            patch_decypharr_config()

        return True, None
    except Exception as e:
        return False, f"Error during Decypharr setup: {e}"


def setup_nzbdav(
    process_handler, install_only: bool = False, configure_only: bool = False
):
    config = CONFIG_MANAGER.get("nzbdav")
    if not config:
        return False, "Configuration for NzbDAV not found."

    logger.info("Starting NzbDAV setup...")

    try:
        if install_only and configure_only:
            return False, "Invalid NzbDAV setup phase."
        nzbdav_config_dir = config.get("config_dir", "/nzbdav")
        nzbdav_config_file = config.get("config_file")
        backend_output_dir = config.get("backend_output_dir") or os.path.join(
            nzbdav_config_dir, "app"
        )
        backend_wwwroot = config.get("backend_wwwroot") or os.path.join(
            backend_output_dir, "wwwroot"
        )
        config_path = config.get("env", {}).get("CONFIG_PATH") or nzbdav_config_dir
        if not os.path.exists(nzbdav_config_dir):
            logger.debug(f"Creating NzbDAV config directory at {nzbdav_config_dir}")
            os.makedirs(nzbdav_config_dir, exist_ok=True)
        chown_single(nzbdav_config_dir, user_id, group_id)
        os.makedirs(config_path, exist_ok=True)
        _chown_recursive_if_needed(config_path, user_id, group_id)

        backend_project_path, _ = _find_nzbdav_backend_project(
            nzbdav_config_dir, config
        )
        if not backend_project_path or not os.path.exists(backend_project_path):
            if configure_only:
                return False, "NzbDAV project not installed."
            logger.warning(
                f"NzbDAV project not found at {nzbdav_config_dir}. Downloading..."
            )
            if not config.get("branch_enabled"):
                release = (
                    config.get("release_version")
                    if config.get("release_version_enabled")
                    else "latest"
                )
                version_to_write = release
                if release == "latest":
                    latest, error = downloader.get_latest_release(
                        repo_owner=config.get("repo_owner"),
                        repo_name=config.get("repo_name"),
                    )
                    if not latest:
                        return False, f"Failed to get latest release: {error}"
                    version_to_write = latest

                if config.get("clear_on_update"):
                    exclude_dirs = config.get("exclude_dirs", [])
                    success, error = clear_directory(nzbdav_config_dir, exclude_dirs)
                    if not success:
                        return False, f"Failed to clear directory: {error}"

                success, error = downloader.download_release_version(
                    process_name=config.get("process_name"),
                    key="nzbdav",
                    repo_owner=config.get("repo_owner"),
                    repo_name=config.get("repo_name"),
                    release_version=release,
                    target_dir=nzbdav_config_dir,
                )
                if not success:
                    return False, f"Failed to download NzbDAV: {error}"

                versions.version_write(
                    process_name=config.get("process_name"),
                    key="nzbdav",
                    version_path=os.path.join(nzbdav_config_dir, "version.txt"),
                    version=version_to_write,
                )

            backend_project_path, error = _find_nzbdav_backend_project(
                nzbdav_config_dir, config
            )
            if not backend_project_path or not os.path.exists(backend_project_path):
                return (
                    False,
                    error or "NzbDAV backend project not found after download.",
                )

        needs_patch = _nzbdav_resource_patch_needed(backend_project_path)
        build_needed = needs_patch
        backend_command, _ = _nzbdav_build_command(backend_output_dir)
        if not backend_command or not os.path.isdir(backend_output_dir):
            build_needed = True
        else:
            frontend_dir = _find_nzbdav_frontend_dir(nzbdav_config_dir, config)
            if frontend_dir and not os.path.isdir(backend_wwwroot):
                build_needed = True

        if build_needed and not configure_only:
            success, error = setup_nzbdav_build(process_handler, config)
            if not success:
                return False, error
        elif build_needed and configure_only:
            return False, "NzbDAV build output missing during configure phase."

        if not install_only:
            backend_command, error = _nzbdav_build_command(backend_output_dir)
            if not backend_command:
                return False, error
            frontend_dir = _find_nzbdav_frontend_dir(nzbdav_config_dir, config)
            script_path = _write_nzbdav_start_script(
                nzbdav_config_dir,
                backend_command,
                frontend_dir,
                int(config.get("backend_port", 8080)),
            )
            config["command"] = ["/bin/sh", script_path]

        if nzbdav_config_file and os.path.exists(nzbdav_config_file):
            chown_single(nzbdav_config_file, user_id, group_id)

        if install_only:
            logger.info("NzbDAV install phase: skipping runtime config patch.")
        elif configure_only:
            try:
                from utils.nzbdav_settings import patch_nzbdav_config

                patched, err = patch_nzbdav_config()
                if not patched and err:
                    logger.warning("NzbDAV post-setup config patch failed: %s", err)
            except Exception as e:
                logger.warning("NzbDAV post-setup config patch skipped: %s", e)

        return True, None
    except Exception as e:
        return False, f"Error during NzbDAV setup: {e}"


def setup_nzbdav_build(process_handler, config):
    nzbdav_config_dir = config.get("config_dir", "/nzbdav")
    backend_output_dir = config.get("backend_output_dir") or os.path.join(
        nzbdav_config_dir, "app"
    )
    backend_wwwroot = config.get("backend_wwwroot") or os.path.join(
        backend_output_dir, "wwwroot"
    )

    backend_project_path, error = _find_nzbdav_backend_project(
        nzbdav_config_dir, config
    )
    if not backend_project_path:
        return False, error

    frontend_dir = _find_nzbdav_frontend_dir(nzbdav_config_dir, config)
    backend_project_dir = os.path.dirname(backend_project_path)
    chown_recursive(backend_project_dir, user_id, group_id)
    patched, patch_error = _patch_nzbdav_embedded_resource_util(backend_project_path)
    if not patched and patch_error:
        logger.warning("NzbDAV resource patch skipped: %s", patch_error)

    platforms = ["dotnet"]
    if frontend_dir:
        platforms.insert(0, "pnpm")

    logger.info("Setting up NzbDAV build environment...")
    dotnet_home = os.path.join(nzbdav_config_dir, ".dotnet")
    nuget_dir = os.path.join(nzbdav_config_dir, ".nuget", "packages")
    os.makedirs(dotnet_home, exist_ok=True)
    os.makedirs(os.path.dirname(nuget_dir), exist_ok=True)
    chown_recursive(dotnet_home, user_id, group_id)
    chown_recursive(os.path.dirname(nuget_dir), user_id, group_id)
    dotnet_env = {
        "HOME": nzbdav_config_dir,
        "DOTNET_CLI_HOME": dotnet_home,
        "NUGET_PACKAGES": nuget_dir,
    }
    success, error = setup_environment(
        process_handler,
        "nzbdav",
        platforms,
        nzbdav_config_dir,
        dotnet_options={
            "project_paths": [backend_project_path],
            "output_dir": backend_output_dir,
            "restore_project_path": backend_project_path,
            "env": dotnet_env,
        },
    )
    if not success:
        return False, error

    static_files_src = os.path.join(backend_project_dir, "WebDav", "StaticFiles")
    static_files_dst = os.path.join(backend_output_dir, "WebDav", "StaticFiles")
    if os.path.isdir(static_files_src):
        if os.path.exists(static_files_dst):
            shutil.rmtree(static_files_dst)
        os.makedirs(os.path.dirname(static_files_dst), exist_ok=True)
        shutil.copytree(static_files_src, static_files_dst)
        logger.info("Copied NzbDAV WebDav static files into publish output.")

    config_template_path = os.path.join(
        backend_project_dir,
        "Api",
        "SabControllers",
        "GetConfig",
        "config_template.json",
    )
    if os.path.isfile(config_template_path):
        shutil.copy2(
            config_template_path,
            os.path.join(backend_output_dir, "config_template.json"),
        )
        chown_single(
            os.path.join(backend_output_dir, "config_template.json"), user_id, group_id
        )

    if frontend_dir:
        success, error = _run_pnpm_script(
            process_handler, frontend_dir, "build:server", "pnpm_build_server"
        )
        if not success:
            return False, error

    if frontend_dir:
        frontend_build_dir = _find_nzbdav_frontend_build_dir(frontend_dir, config)
        if frontend_build_dir:
            if os.path.exists(backend_wwwroot):
                shutil.rmtree(backend_wwwroot)
            shutil.copytree(frontend_build_dir, backend_wwwroot)
            logger.info("Copied NzbDAV frontend build into backend wwwroot.")
        else:
            logger.warning("Frontend build output not found. Skipping wwwroot copy.")

    return True, None


def _run_pnpm_script(process_handler, config_dir, script_name, process_name):
    package_json_path = os.path.join(config_dir, "package.json")
    if not os.path.isfile(package_json_path):
        return True, None

    try:
        import json

        with open(package_json_path, "r") as f:
            package_data = json.load(f)
        scripts = package_data.get("scripts", {})
        if script_name not in scripts:
            return True, None
    except Exception as e:
        return False, f"Failed to read package.json: {e}"

    if script_name == "build:server":
        dist_node = os.path.join(config_dir, "dist-node")
        os.makedirs(dist_node, exist_ok=True)
        chown_recursive(dist_node, user_id, group_id)

    env = os.environ.copy()
    env["HOME"] = config_dir
    env["npm_config_userconfig"] = os.path.join(config_dir, ".npmrc")

    process_handler.start_process(
        process_name, config_dir, ["pnpm", "run", script_name], env=env
    )
    process_handler.wait(process_name)
    if process_handler.returncode != 0:
        return False, f"Error running pnpm {script_name}: {process_handler.stderr}"
    return True, None


def _patch_nzbdav_embedded_resource_util(backend_project_path):
    util_path = os.path.join(
        os.path.dirname(backend_project_path), "Utils", "EmbeddedResourceUtil.cs"
    )
    if not os.path.isfile(util_path):
        return False, f"EmbeddedResourceUtil.cs not found at {util_path}"

    try:
        with open(util_path, "r") as f:
            contents = f.read()
    except OSError as e:
        return False, f"Failed to read {util_path}: {e}"

    if "AppContext.BaseDirectory" in contents and "GetExecutingAssembly" in contents:
        return True, None

    if "using System.IO;" not in contents:
        if "using System.Diagnostics;" in contents:
            contents = contents.replace(
                "using System.Diagnostics;",
                "using System.Diagnostics;\nusing System.IO;",
            )
        else:
            contents = "using System.IO;\n" + contents

    old = (
        "    public static Stream GetStream(string resourcePath)\n"
        "    {\n"
        "        var assembly = Assembly.GetCallingAssembly();\n"
        "        var fullResourcePath = GetFullResourcePath(resourcePath);\n"
        "        return assembly.GetManifestResourceStream(fullResourcePath)!;\n"
        "    }\n"
    )
    new = (
        "    public static Stream GetStream(string resourcePath)\n"
        "    {\n"
        "        var fullResourcePath = GetFullResourcePath(resourcePath);\n"
        "        var assembly = Assembly.GetExecutingAssembly();\n"
        "        var stream = assembly.GetManifestResourceStream(fullResourcePath);\n"
        "        if (stream != null)\n"
        "            return stream;\n"
        "\n"
        "        assembly = Assembly.GetCallingAssembly();\n"
        "        stream = assembly.GetManifestResourceStream(fullResourcePath);\n"
        "        if (stream != null)\n"
        "            return stream;\n"
        "\n"
        "        var fallbackPath = Path.Combine(AppContext.BaseDirectory, resourcePath);\n"
        "        if (File.Exists(fallbackPath))\n"
        "            return File.OpenRead(fallbackPath);\n"
        "\n"
        "        var lastDot = resourcePath.LastIndexOf('.');\n"
        "        if (lastDot > 0)\n"
        "        {\n"
        "            var pathPart = resourcePath.Substring(0, lastDot).Replace('.', Path.DirectorySeparatorChar);\n"
        "            var extPart = resourcePath.Substring(lastDot + 1);\n"
        '            var dottedPath = $"{pathPart}.{extPart}";\n'
        "            var dottedFallback = Path.Combine(AppContext.BaseDirectory, dottedPath);\n"
        "            if (File.Exists(dottedFallback))\n"
        "                return File.OpenRead(dottedFallback);\n"
        "\n"
        '            var webDavFallback = Path.Combine(AppContext.BaseDirectory, "WebDav", dottedPath);\n'
        "            if (File.Exists(webDavFallback))\n"
        "                return File.OpenRead(webDavFallback);\n"
        "        }\n"
        "\n"
        '        throw new FileNotFoundException($"Embedded resource not found: {fullResourcePath}");\n'
        "    }\n"
    )

    if old not in contents:
        return (
            False,
            "EmbeddedResourceUtil.cs signature did not match expected template.",
        )

    contents = contents.replace(old, new)
    try:
        with open(util_path, "w") as f:
            f.write(contents)
    except OSError as e:
        return False, f"Failed to write {util_path}: {e}"

    return True, None


def _nzbdav_resource_patch_needed(backend_project_path):
    util_path = os.path.join(
        os.path.dirname(backend_project_path), "Utils", "EmbeddedResourceUtil.cs"
    )
    if not os.path.isfile(util_path):
        return False

    try:
        with open(util_path, "r") as f:
            contents = f.read()
    except OSError:
        return False

    return (
        "AppContext.BaseDirectory" not in contents
        or "GetExecutingAssembly" not in contents
    )


def _write_nzbdav_start_script(config_dir, backend_command, frontend_dir, backend_port):
    script_path = os.path.join(config_dir, "nzbdav_start.sh")
    backend_cmd = " ".join(shlex.quote(part) for part in backend_command)
    script_lines = [
        "#!/bin/sh",
        "set -e",
        f'BACKEND_PORT="${{BACKEND_PORT:-{backend_port}}}"',
        'BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:$BACKEND_PORT}"',
        "export BACKEND_URL",
        f"{backend_cmd} --db-migration",
        f"{backend_cmd} &",
        "BACKEND_PID=$!",
    ]
    if frontend_dir:
        script_lines.extend(
            [
                f"cd {shlex.quote(frontend_dir)}",
                "node dist-node/server.js &",
                "FRONTEND_PID=$!",
            ]
        )
    script_lines.extend(
        [
            "terminate() {",
            '  if [ -n "$BACKEND_PID" ]; then kill "$BACKEND_PID" 2>/dev/null || true; fi',
            '  if [ -n "$FRONTEND_PID" ]; then kill "$FRONTEND_PID" 2>/dev/null || true; fi',
            "  wait",
            "}",
            "trap terminate TERM INT",
        ]
    )
    if frontend_dir:
        script_lines.extend(
            [
                "wait $BACKEND_PID",
                "EXIT_CODE=$?",
                "kill $FRONTEND_PID 2>/dev/null || true",
                "wait $FRONTEND_PID 2>/dev/null || true",
                "exit $EXIT_CODE",
            ]
        )
    else:
        script_lines.append("wait $BACKEND_PID")

    with open(script_path, "w") as f:
        f.write("\n".join(script_lines) + "\n")
    os.chmod(script_path, 0o755)
    chown_single(script_path, user_id, group_id)
    return script_path


def _find_nzbdav_backend_project(config_dir, config):
    candidates = []
    for root, dirs, files in os.walk(config_dir):
        dirs[:] = [
            d
            for d in dirs
            if d not in ("node_modules", "bin", "obj", ".git", ".pnpm-store", ".venv")
        ]
        for file in files:
            if file.endswith(".csproj"):
                candidates.append(os.path.join(root, file))

    if not candidates:
        return None, "No .csproj files found for NzbDAV backend."

    scored = []
    for path in candidates:
        lower = path.lower()
        score = 0
        if any(token in lower for token in ("api", "server", "backend", "web")):
            score += 2
        if "nzbdav" in lower:
            score += 1
        scored.append((score, len(path), path))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[0][2], None


def _find_nzbdav_frontend_dir(config_dir, config):
    candidates = []
    for root, dirs, files in os.walk(config_dir):
        dirs[:] = [
            d
            for d in dirs
            if d not in ("node_modules", "bin", "obj", ".git", ".pnpm-store", ".venv")
        ]
        if "package.json" in files:
            candidates.append(root)

    if not candidates:
        return None

    scored = []
    for path in candidates:
        lower = path.lower()
        score = 0
        if any(token in lower for token in ("frontend", "client", "web", "ui")):
            score += 2
        scored.append((score, len(path), path))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored[0][2]


def _find_nzbdav_frontend_build_dir(frontend_dir, config):
    for candidate in ("build/client", "dist", "build", "wwwroot"):
        path = os.path.join(frontend_dir, *candidate.split("/"))
        if os.path.exists(path):
            return path
    return None


def _nzbdav_build_command(backend_output_dir):
    if not os.path.isdir(backend_output_dir):
        return None, f"NzbDAV output directory not found: {backend_output_dir}"

    binaries = []
    dlls = []
    for entry in os.listdir(backend_output_dir):
        path = os.path.join(backend_output_dir, entry)
        if not os.path.isfile(path):
            continue
        if entry.endswith(".dll"):
            dlls.append(path)
        elif os.access(path, os.X_OK) and "." not in entry:
            binaries.append(path)

    if binaries:
        return [binaries[0]], None
    if dlls:
        return ["dotnet", dlls[0]], None

    return None, "NzbDAV publish output did not include an executable or DLL."


def build_decypharr_dev(process_handler, config):
    if not config:
        return False, "Configuration for Decypharr not found."
    config_dir = config.get("config_dir", "/decypharr")
    logger.info("Building Decypharr development environment...")
    try:
        success, error = setup_pnpm_environment(process_handler, config_dir)
        if not success:
            return False, f"Failed to set up pnpm environment: {error}"
        branch = config.get("branch")
        version = "3.0.0"  # Default version until it can be determined dynamically
        command = [
            "go",
            "build",
            "-ldflags",
            f"-X github.com/sirrobot01/decypharr/pkg/version.Version={version} -X github.com/sirrobot01/decypharr/pkg/version.Channel={branch}",
            "-o",
            "decypharr",
            ".",
        ]
        for attempt in range(3):
            process_handler.start_process("go_build", config_dir, command)
            process_handler.wait("go_build")
            if process_handler.returncode == 0:
                break
            logger.warning(f"Decypharr build failed (attempt {attempt + 1}/3)")

        logger.info("Decypharr development environment built successfully.")
        return True, None
    except Exception as e:
        return False, f"Error during Decypharr development environment setup: {e}"


def plex_debrid_setup():
    config = CONFIG_MANAGER.get("plex_debrid")
    if not config:
        return False, "Configuration for Plex Debrid not found."

    if not os.path.exists(config["config_file"]):
        logger.debug(
            f"Copying settings-default.json from {config['config_dir']} to {config['config_file']}"
        )
        shutil.copy(
            os.path.join(config["config_dir"], "settings-default.json"),
            config["config_file"],
        )
        chown_recursive(os.path.join(config["config_dir"], "config"), user_id, group_id)

    trakt_file = os.path.join(config["config_dir"], "content", "services", "trakt.py")
    if os.path.exists(trakt_file):
        with open(trakt_file, "r") as f:
            trakt_contents = f.read()

        updated_trakt_contents = re.sub(
            r'env_file\s*=\s*[\'"]\.env[\'"]',
            'env_file = "./config/.env"',
            trakt_contents,
        )

        with open(trakt_file, "w") as f:
            f.write(updated_trakt_contents)

        logger.debug("Updated env_file path in trakt.py to './config/.env'")
    return True, None


def dumb_frontend_setup():
    dumb_config = CONFIG_MANAGER.get("dumb")
    config = dumb_config.get("frontend")
    if not config:
        return False, "Configuration for DUMB Frontend not found."
    api_config = dumb_config.get("api_service", {})
    if not api_config:
        return False, "Configuration for API Service not found."
    frontend_host = config.get("host", "127.0.0.1")
    frontend_port = str(config.get("port", 3005))
    api_host = api_config.get("host", "127.0.0.1")
    api_port = str(api_config.get("port", 8000))
    api_url = f"http://{api_host}:{api_port}"
    env_vars = {
        **config.get("env", {}),
        "HOST": frontend_host,
        "PORT": frontend_port,
        "DUMB_API_URL": api_url,
    }
    config["env"] = env_vars
    return True, None


def phalanx_setup(
    process_handler, install_only: bool = False, configure_only: bool = False
):
    config = CONFIG_MANAGER.get("phalanx_db")
    if not config:
        return False, "Configuration for Phalanx not found."

    logger.info("Starting Phalanx setup...")

    try:
        if install_only and configure_only:
            return False, "Invalid Phalanx setup phase."
        phalanx_config_dir = config.get("config_dir")
        phalanx_data_dir = os.path.join(phalanx_config_dir, "data")
        original_package_file = os.path.join(phalanx_config_dir, "package.json")
        platforms = config.get("platforms", [])

        if not os.path.isfile(original_package_file):
            if configure_only:
                return False, "Phalanx DB not installed."
            logger.warning(
                f"Phalanx project not found at {phalanx_config_dir}. Downloading..."
            )
            release, error = downloader.get_latest_release(
                repo_owner=config.get("repo_owner"),
                repo_name=config.get("repo_name"),
            )
            if not release:
                return False, f"Failed to get latest release: {error}"
            logger.info(
                f"Downloading Phalanx release {release} from {config.get('repo_owner')}/{config.get('repo_name')}"
            )
            success, error = downloader.download_release_version(
                process_name=config.get("process_name"),
                key="phalanx_db",
                repo_owner=config.get("repo_owner"),
                repo_name=config.get("repo_name"),
                release_version=release,
                target_dir=phalanx_config_dir,
            )
            if not success:
                return False, f"Failed to download Phalanx DB: {error}"
            if platforms:
                success, error = setup_environment(
                    process_handler, "phalanx_db", platforms, phalanx_config_dir
                )
                if not success:
                    return False, f"Failed to set up environment: {error}"

        if install_only:
            return True, None

        for subdir in ["db_data", "p2p-db-storage", "logs", "autobase_storage_v4"]:
            target_path = os.path.join(phalanx_data_dir, subdir)
            symlink_path = os.path.join(phalanx_config_dir, subdir)

            os.makedirs(target_path, exist_ok=True)

            if os.path.islink(symlink_path):
                if not os.path.exists(os.readlink(symlink_path)):
                    logger.warning(
                        f"Broken symlink detected at {symlink_path}. Recreating..."
                    )
                    os.remove(symlink_path)
                    os.symlink(target_path, symlink_path)
            elif os.path.exists(symlink_path):
                logger.warning(
                    f"Expected symlink at {symlink_path}, but found real file/dir. Skipping."
                )
            else:
                os.symlink(target_path, symlink_path)

        logs_dir = os.path.join(phalanx_data_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)

        autobase_storage_dir = os.path.join(
            phalanx_config_dir, "autobase_storage_v4", "db"
        )
        if not os.path.exists(autobase_storage_dir):
            logger.debug(
                f"Creating autobase storage directory at {autobase_storage_dir}"
            )
            os.makedirs(autobase_storage_dir, exist_ok=True)

        chown_recursive(
            phalanx_data_dir, CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
        )

        port = str(config.get("port", 8888))
        debug = (
            "true" if config.get("log_level", "debug").lower() == "debug" else "false"
        )
        env_vars = {
            **config.get("env", {}),
            "PORT": port,
            "DEBUG": debug,
        }
        config["env"] = env_vars

        js_files = glob.glob(os.path.join(phalanx_config_dir, "phalanx_db_rest_v*.js"))

        def extract_version(path):
            match = re.search(r"v(\d+)", os.path.basename(path))
            return int(match.group(1)) if match else -1

        if js_files:
            js_files.sort(key=extract_version, reverse=True)
            latest_file = os.path.basename(js_files[0])
            config["command"] = ["node", latest_file]
            logger.debug(f"Resolved Phalanx command to: {config['command']}")
        else:
            logger.warning(
                "No versioned phalanx_db_rest_v*.js found. Leaving command as-is."
            )

        logger.info("Phalanx setup complete.")
        return True, None

    except Exception as e:
        return False, f"Error during Phalanx setup: {e}"


def setup_plex(install_only: bool = False, configure_only: bool = False):
    config = CONFIG_MANAGER.get("plex")

    if not config or not config.get("enabled"):
        logger.info("Plex is disabled. Skipping setup.")
        return True, None

    def normalize_version(version):
        if not version:
            return version
        return version[1:] if version.startswith("v") else version

    if install_only and configure_only:
        return False, "Invalid Plex setup phase."

    plex_media_server_dir = config.get(
        "plex_media_server_dir", "/usr/lib/plexmediaserver"
    )
    pinned_version = config.get("pinned_version")
    installer = PlexInstaller()
    if not os.path.exists(plex_media_server_dir):
        logger.warning(
            f"Plex Media Server directory {plex_media_server_dir} does not exist. Installing Plex Media Server..."
        )
        success, error = installer.install_plex_media_server(
            version=normalize_version(pinned_version) if pinned_version else None
        )
        if not success:
            logger.error(f"Plex install failed: {error}")
            return False, error
    elif pinned_version:
        current_version, error = versions.version_check(
            config.get("process_name", "Plex Media Server"), None, "plex"
        )
        current_version = normalize_version(current_version)
        target_version = normalize_version(pinned_version)
        if not current_version:
            logger.warning(
                f"Failed to read current Plex version for pin check: {error}"
            )
        elif current_version != target_version:
            logger.info(
                f"Plex pinned to {target_version}; installing over {current_version}."
            )
            success, error = installer.install_plex_media_server(version=target_version)
            if not success:
                logger.error(f"Plex pinned install failed: {error}")
                return False, error

    if install_only:
        return True, None

    os.makedirs(config["config_dir"], exist_ok=True)
    if os.stat(config["config_dir"]).st_uid != CONFIG_MANAGER.get("puid"):
        chown_recursive(
            config["config_dir"], CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
        )
    if configure_only and not os.path.exists(plex_media_server_dir):
        return False, f"Plex not installed at {plex_media_server_dir}."
    pid_path = os.path.join(
        config["config_dir"], "Plex Media Server", "plexmediaserver.pid"
    )
    if os.path.exists(pid_path):
        try:
            os.remove(pid_path)
            logger.info("Removed stale Plex PID file at %s", pid_path)
        except Exception as e:
            logger.warning("Failed to remove Plex PID file at %s: %s", pid_path, e)
    dbrepair_cfg = config.get("dbrepair", {}) or {}
    dbrepair_dir = dbrepair_cfg.get("install_dir", "/data/dbrepair")
    try:
        os.makedirs(dbrepair_dir, exist_ok=True)
        if os.stat(dbrepair_dir).st_uid != CONFIG_MANAGER.get("puid"):
            chown_recursive(
                dbrepair_dir, CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
            )
    except Exception as e:
        logger.warning("Failed to prepare DBRepair dir %s: %s", dbrepair_dir, e)
    logger.info("Setting up Plex Media Server environment...")
    env_vars = {
        "PLEX_MEDIA_SERVER_APPLICATION_SUPPORT_DIR": config["config_dir"],
        "PLEX_MEDIA_SERVER_HOME": "/usr/lib/plexmediaserver",
        "PLEX_MEDIA_SERVER_INFO_VENDOR": "Docker",
        "PLEX_MEDIA_SERVER_INFO_MODEL": "Linux",
        "PLEX_MEDIA_SERVER_INFO_PLATFORM": "Docker",
        "LD_LIBRARY_PATH": "/usr/lib/plexmediaserver",
        "TMPDIR": "/tmp",
    }
    config["env"] = env_vars
    plex_claim = config.get("plex_claim", "")
    preferences_path = config.get("config_file")
    if plex_claim and not os.path.exists(preferences_path):
        from utils.plex import perform_plex_claim

        success, error = perform_plex_claim(plex_claim, preferences_path, logger)
        if not success:
            return False, f"Failed to claim Plex server: {error}"
        chown_recursive(
            config["config_dir"], CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
        )
    elif plex_claim and os.path.exists(preferences_path):
        with open(preferences_path) as f:
            if "plexOnlineToken" in f.read():
                logger.info(f"Plex server already claimed. Skipping PLEX_CLAIM.")
            else:
                from utils.plex import perform_plex_claim

                logger.debug("Claiming Plex server with provided PLEX_CLAIM token...")
                success, error = perform_plex_claim(
                    plex_claim, preferences_path, logger
                )
                if not success:
                    return False, f"Failed to claim Plex server: {error}"
                if os.stat(config["config_dir"]).st_uid != CONFIG_MANAGER.get("puid"):
                    chown_recursive(
                        config["config_dir"],
                        CONFIG_MANAGER.get("puid"),
                        CONFIG_MANAGER.get("pgid"),
                    )
    dbrepair_cfg = config.get("dbrepair", {}) or {}
    if dbrepair_cfg.get("enabled") and dbrepair_cfg.get("run_before_start"):
        from utils.plex_dbrepair import run_dbrepair_once

        if not run_dbrepair_once(run_before_start=True):
            logger.warning("DBRepair pre-start run skipped or failed.")
    command = ["/usr/lib/plexmediaserver/Plex Media Server"]
    config["command"] = command
    return True, None


def _configure_tautulli_plex(config_file):
    plex_config = CONFIG_MANAGER.get("plex", {})
    if not plex_config.get("enabled"):
        return False, None
    logger.info("Configuring Tautulli with Plex settings...")
    plex_address = CONFIG_MANAGER.get("dumb", {}).get("plex_address") or ""
    plex_token = CONFIG_MANAGER.get("dumb", {}).get("plex_token") or ""
    plex_identifier = ""
    plex_port = str(plex_config.get("port", 32400))
    plex_name = plex_config.get("friendly_name") or plex_config.get(
        "process_name", "Plex Media Server"
    )
    plex_prefs_path = plex_config.get(
        "config_file", "/plex/Plex Media Server/Preferences.xml"
    )
    if os.path.exists(plex_prefs_path):
        try:
            tree = ET.parse(plex_prefs_path)
            root = tree.getroot()
            plex_identifier = (
                root.attrib.get("MachineIdentifier")
                or root.attrib.get("ProcessedMachineIdentifier")
                or ""
            )
            if not plex_token:
                plex_token = root.attrib.get("PlexOnlineToken") or plex_token
        except Exception as e:
            logger.warning("Failed to read Plex preferences for Tautulli: %s", e)

    if plex_address:
        parsed = urllib.parse.urlparse(plex_address)
        plex_scheme = parsed.scheme or "http"
        plex_host = parsed.hostname or parsed.path or "127.0.0.1"
        plex_port = str(parsed.port or plex_port)
        plex_url = f"{plex_scheme}://{plex_host}:{plex_port}"
    else:
        plex_host = "127.0.0.1"
        plex_url = f"http://{plex_host}:{plex_port}"

    if not os.path.exists(config_file):
        return False, None

    desired = {
        "pms_token": plex_token,
        "pms_url": plex_url,
        "pms_url_manual": "1" if plex_url else "",
        "pms_name": plex_name,
        "pms_ip": plex_host,
        "pms_port": plex_port,
        "pms_identifier": plex_identifier,
    }

    with open(config_file, "r") as f:
        lines = f.readlines()

    pms_start = None
    pms_end = None
    section_re = re.compile(r"^\s*\[[^\]]+\]\s*$")
    for idx, line in enumerate(lines):
        if line.strip().lower() == "[pms]":
            pms_start = idx
            continue
        if pms_start is not None and idx > pms_start and section_re.match(line):
            pms_end = idx
            break

    if pms_start is None:
        pms_start = len(lines)
        pms_end = len(lines)
        lines.append("\n" if lines and not lines[-1].endswith("\n") else "")
        lines.append("[PMS]\n")
        pms_start = len(lines) - 1
        pms_end = len(lines)

    if pms_end is None:
        pms_end = len(lines)

    key_line_index = {}
    for idx in range(pms_start + 1, pms_end):
        line = lines[idx]
        if "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            key_line_index[key] = idx

    changed = False
    insert_lines = []

    def _is_blank(value: str) -> bool:
        stripped = value.strip()
        return stripped == "" or stripped in ('""', "''")

    for key, value in desired.items():
        if not value:
            continue
        if key in key_line_index:
            idx = key_line_index[key]
            current = lines[idx].split("=", 1)[1].strip()
            if _is_blank(current):
                lines[idx] = f"{key} = {value}\n"
                changed = True
        else:
            insert_lines.append(f"{key} = {value}\n")
            changed = True

    if insert_lines:
        lines[pms_end:pms_end] = insert_lines

    if not changed:
        logger.info("Tautulli Plex settings are already up to date.")
        return False, None

    with open(config_file, "w") as f:
        f.writelines(lines)
    _chown_recursive_if_needed(
        os.path.dirname(config_file),
        CONFIG_MANAGER.get("puid"),
        CONFIG_MANAGER.get("pgid"),
    )
    logger.info("Tautulli Plex settings updated.")
    return True, None


def setup_tautulli(
    process_handler, install_only: bool = False, configure_only: bool = False
):
    config = CONFIG_MANAGER.get("tautulli")

    if not config or not config.get("enabled"):
        logger.info("Tautulli is disabled. Skipping setup.")
        return True, None

    config_dir = config.get("config_dir", "/tautulli")
    config_file = config.get("config_file", "/tautulli/data/config.ini")
    data_dir = os.path.dirname(config_file)
    log_file = config.get("log_file", "/tautulli/data/logs/tautulli.log")
    log_dir = os.path.dirname(log_file)
    tautulli_py_path = os.path.join(config_dir, "Tautulli.py")

    for path in [config_dir, data_dir, log_dir]:
        os.makedirs(path, exist_ok=True)
        _chown_recursive_if_needed(
            path, CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
        )

    if install_only and configure_only:
        return False, "Invalid Tautulli setup phase."

    needs_download = not os.path.isfile(tautulli_py_path)
    if needs_download and configure_only:
        return False, f"Tautulli not installed at {tautulli_py_path}."

    if needs_download:
        logger.warning("Tautulli not found at %s. Downloading...", tautulli_py_path)
        exclude_dirs = None
        if config.get("clear_on_update"):
            exclude_dirs = config.get("exclude_dirs", [])
            success, error = clear_directory(config_dir, exclude_dirs)
            if not success:
                return False, f"Failed to clear directory: {error}"

        if config.get("branch_enabled"):
            branch = config.get("branch", "master")
            branch_url, zip_folder_name = downloader.get_branch(
                config.get("repo_owner"),
                config.get("repo_name"),
                branch,
            )
            if not branch_url:
                return False, f"Failed to fetch branch {branch}"
            success, error = downloader.download_and_extract(
                branch_url,
                config_dir,
                zip_folder_name=zip_folder_name,
                exclude_dirs=exclude_dirs,
            )
            if not success:
                return False, f"Failed to download Tautulli branch: {error}"
        else:
            release_version = config.get("release_version", "latest")
            version_to_write = release_version
            if release_version.lower() == "latest":
                latest_version, latest_error = downloader.get_latest_release(
                    config.get("repo_owner"),
                    config.get("repo_name"),
                    nightly=False,
                )
                if latest_version:
                    version_to_write = latest_version
                else:
                    logger.warning(
                        "Failed to resolve latest Tautulli version: %s", latest_error
                    )
            success, error = downloader.download_release_version(
                process_name=config.get("process_name"),
                key="tautulli",
                repo_owner=config.get("repo_owner"),
                repo_name=config.get("repo_name"),
                release_version=release_version,
                target_dir=config_dir,
                exclude_dirs=exclude_dirs,
            )
            if not success:
                return False, f"Failed to download Tautulli release: {error}"
            versions.version_write(
                config.get("process_name"),
                key="tautulli",
                version_path=os.path.join(config_dir, "version.txt"),
                version=version_to_write,
            )

    if config.get("platforms") and not configure_only:
        venv_path = os.path.join(config_dir, "venv")
        if not os.path.isdir(venv_path):
            success, error = setup_environment(
                process_handler,
                "tautulli",
                config.get("platforms"),
                config_dir,
            )
            if not success:
                return False, f"Failed to set up environment for Tautulli: {error}"

    if install_only:
        return True, None
    # updated, error = _configure_tautulli_plex(config_file)
    # if error:
    #    return False, error
    # if updated:
    #    logger.info("Configured Tautulli Plex settings from Plex config.")

    command = config.get("command", [])
    port = str(config.get("port", 8181))
    if isinstance(command, list):
        for i, arg in enumerate(command):
            if arg in ("-p", "--port") and i + 1 < len(command):
                if command[i + 1] != "{port}":
                    command[i + 1] = "{port}"
                break
        command = [
            arg.replace("{port}", port) if "{port}" in arg else arg for arg in command
        ]
        config["command"] = command

    logger.info("Tautulli setup complete.")
    return True, None


def setup_seerr(
    process_handler, install_only: bool = False, configure_only: bool = False
):
    config = CONFIG_MANAGER.get("seerr", {})
    if not config:
        return False, "Seerr configuration not found."

    instances = config.get("instances", {})
    if not instances:
        logger.info("No Seerr instances configured.")
        return True, None

    if install_only and configure_only:
        return False, "Invalid Seerr setup phase."

    for instance_name, instance in instances.items():
        if not instance.get("enabled", False):
            logger.debug("Skipping disabled Seerr instance: %s", instance_name)
            continue

        instance_config_dir = (
            instance.get("config_dir") or f"/seerr/{instance_name.lower()}"
        )
        instance["config_dir"] = instance_config_dir
        path_changed = False

        def _rewrite_seerr_path(value):
            if isinstance(value, str) and value.startswith("/seerr/default"):
                return value.replace("/seerr/default", instance_config_dir, 1)
            return value

        exclude_dirs = instance.get("exclude_dirs", [])
        if exclude_dirs:
            rewritten = []
            for path in exclude_dirs:
                rewritten.append(_rewrite_seerr_path(path))
            if rewritten != exclude_dirs:
                instance["exclude_dirs"] = rewritten
                path_changed = True

        config_file = instance.get("config_file")
        new_config_file = _rewrite_seerr_path(config_file)
        if new_config_file != config_file:
            instance["config_file"] = new_config_file
            path_changed = True

        log_file = instance.get("log_file")
        new_log_file = _rewrite_seerr_path(log_file)
        if new_log_file != log_file:
            instance["log_file"] = new_log_file
            path_changed = True
        instance_env = instance.get("env", {}) or {}
        env_changed = False
        new_port = str(instance.get("port", 5055))
        if instance_env.get("PORT") != new_port:
            instance_env["PORT"] = new_port
            env_changed = True
        if "NODE_ENV" not in instance_env:
            instance_env["NODE_ENV"] = "production"
            env_changed = True
        instance["env"] = instance_env
        entry_path = os.path.join(instance_config_dir, "dist", "index.js")
        repo_marker = os.path.join(instance_config_dir, "package.json")
        if not install_only and (env_changed or path_changed):
            CONFIG_MANAGER.save_config(instance.get("process_name"))

        os.makedirs(instance_config_dir, exist_ok=True)
        _chown_recursive_if_needed(
            instance_config_dir, CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
        )
        real_config_dir = os.path.realpath(instance_config_dir)
        if real_config_dir != instance_config_dir:
            _chown_recursive_if_needed(
                real_config_dir, CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
            )

        needs_download = not os.path.isfile(repo_marker)
        if needs_download and configure_only:
            return False, f"Seerr instance {instance_name} not installed."
        if needs_download:
            logger.warning(
                "Seerr instance %s not found at %s. Downloading...",
                instance_name,
                repo_marker,
            )
            exclude_dirs = None
            if instance.get("clear_on_update"):
                exclude_dirs = instance.get("exclude_dirs", [])
                success, error = clear_directory(instance_config_dir, exclude_dirs)
                if not success:
                    return False, f"Failed to clear directory: {error}"

            if instance.get("branch_enabled"):
                branch = instance.get("branch", "main")
                branch_url, zip_folder_name = downloader.get_branch(
                    instance.get("repo_owner"),
                    instance.get("repo_name"),
                    branch,
                )
                if not branch_url:
                    return False, f"Failed to fetch branch {branch}"
                success, error = downloader.download_and_extract(
                    branch_url,
                    instance_config_dir,
                    zip_folder_name=zip_folder_name,
                    exclude_dirs=exclude_dirs,
                )
                if not success:
                    return False, f"Failed to download Seerr branch: {error}"
            else:
                release_version = instance.get("release_version", "latest")
                version_to_write = release_version
                if release_version.lower() == "latest":
                    latest_version, latest_error = downloader.get_latest_release(
                        instance.get("repo_owner"),
                        instance.get("repo_name"),
                        nightly=False,
                    )
                    if latest_version:
                        version_to_write = latest_version
                    else:
                        logger.warning(
                            "Failed to resolve latest Seerr version: %s", latest_error
                        )
                success, error = downloader.download_release_version(
                    process_name=instance.get("process_name"),
                    key="seerr",
                    repo_owner=instance.get("repo_owner"),
                    repo_name=instance.get("repo_name"),
                    release_version=release_version,
                    target_dir=instance_config_dir,
                    exclude_dirs=exclude_dirs,
                )
                if not success:
                    return False, f"Failed to download Seerr release: {error}"
                versions.version_write(
                    instance.get("process_name"),
                    key="seerr",
                    version_path=os.path.join(instance_config_dir, "version.txt"),
                    version=version_to_write,
                )

            _chown_recursive_if_needed(
                instance_config_dir,
                CONFIG_MANAGER.get("puid"),
                CONFIG_MANAGER.get("pgid"),
            )

        if instance.get("platforms") and not configure_only:
            node_modules_path = os.path.join(instance_config_dir, "node_modules")
            if (
                needs_download
                or not os.path.isdir(node_modules_path)
                or not os.path.isfile(entry_path)
            ):
                success, error = setup_environment(
                    process_handler,
                    "seerr",
                    instance.get("platforms"),
                    instance_config_dir,
                )
                if not success:
                    return (
                        False,
                        f"Failed to set up environment for Seerr instance {instance_name}: {error}",
                    )
        next_dir = os.path.join(real_config_dir, ".next")
        if os.path.isdir(next_dir):
            _chown_recursive_if_needed(
                next_dir, CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
            )

        if install_only:
            continue

    logger.info("Seerr setup complete.")
    return True, None


def setup_huntarr(
    process_handler, install_only: bool = False, configure_only: bool = False
):
    config = CONFIG_MANAGER.get("huntarr", {})
    if not config:
        return False, "Huntarr configuration not found."

    instances = config.get("instances", {})
    if not instances:
        logger.info("No Huntarr instances configured.")
        return True, None

    if install_only and configure_only:
        return False, "Invalid Huntarr setup phase."

    for instance_name, instance in instances.items():
        if not instance.get("enabled", False):
            logger.debug("Skipping disabled Huntarr instance: %s", instance_name)
            continue

        instance_config_dir = (
            instance.get("config_dir") or f"/huntarr/{instance_name.lower()}"
        )
        instance["config_dir"] = instance_config_dir
        path_changed = False

        def _rewrite_huntarr_path(value):
            if isinstance(value, str) and value.startswith("/huntarr/default"):
                return value.replace("/huntarr/default", instance_config_dir, 1)
            return value

        exclude_dirs = instance.get("exclude_dirs", [])
        if exclude_dirs:
            rewritten = [_rewrite_huntarr_path(path) for path in exclude_dirs]
            if rewritten != exclude_dirs:
                instance["exclude_dirs"] = rewritten
                path_changed = True

        config_file = instance.get("config_file")
        new_config_file = _rewrite_huntarr_path(config_file)
        if new_config_file != config_file:
            instance["config_file"] = new_config_file
            path_changed = True

        log_file = instance.get("log_file")
        new_log_file = _rewrite_huntarr_path(log_file)
        if new_log_file != log_file:
            instance["log_file"] = new_log_file
            path_changed = True

        command = instance.get("command", [])
        if isinstance(command, list):
            updated_command = [_rewrite_huntarr_path(arg) for arg in command]
            if updated_command != command:
                instance["command"] = updated_command
                path_changed = True

        instance_env = instance.get("env", {}) or {}
        env_changed = False
        new_port = str(instance.get("port", 9705))
        if instance_env.get("HUNTARR_PORT") != new_port:
            instance_env["HUNTARR_PORT"] = new_port
            env_changed = True

        config_root = os.path.join(instance_config_dir, "config")
        if instance_env.get("HUNTARR_CONFIG_DIR") != config_root:
            instance_env["HUNTARR_CONFIG_DIR"] = config_root
            env_changed = True

        instance["env"] = instance_env
        if not install_only and (env_changed or path_changed):
            CONFIG_MANAGER.save_config(instance.get("process_name"))

        os.makedirs(instance_config_dir, exist_ok=True)
        os.makedirs(config_root, exist_ok=True)
        log_dir = os.path.join(instance_config_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        backup_dir = os.path.join(config_root, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        _chown_recursive_if_needed(
            instance_config_dir, CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
        )
        _chown_recursive_if_needed(
            config_root, CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
        )
        _chown_recursive_if_needed(
            log_dir, CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
        )
        _chown_recursive_if_needed(
            backup_dir, CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
        )

        repo_marker = os.path.join(instance_config_dir, "main.py")
        needs_download = not os.path.isfile(repo_marker)
        if needs_download and configure_only:
            return False, f"Huntarr instance {instance_name} not installed."
        if needs_download:
            logger.warning(
                "Huntarr instance %s not found at %s. Downloading...",
                instance_name,
                repo_marker,
            )
            exclude_dirs = None
            if instance.get("clear_on_update"):
                exclude_dirs = instance.get("exclude_dirs", [])
                success, error = clear_directory(instance_config_dir, exclude_dirs)
                if not success:
                    return False, f"Failed to clear directory: {error}"

            if instance.get("branch_enabled"):
                branch = instance.get("branch", "main")
                branch_url, zip_folder_name = downloader.get_branch(
                    instance.get("repo_owner"),
                    instance.get("repo_name"),
                    branch,
                )
                if not branch_url:
                    return False, f"Failed to fetch branch {branch}"
                success, error = downloader.download_and_extract(
                    branch_url,
                    instance_config_dir,
                    zip_folder_name=zip_folder_name,
                    exclude_dirs=exclude_dirs,
                )
                if not success:
                    return False, f"Failed to download Huntarr branch: {error}"
            else:
                release_version = instance.get("release_version", "latest")
                version_to_write = release_version
                if release_version.lower() == "latest":
                    latest_version, latest_error = downloader.get_latest_release(
                        instance.get("repo_owner"),
                        instance.get("repo_name"),
                        nightly=False,
                    )
                    if latest_version:
                        version_to_write = latest_version
                    else:
                        logger.warning(
                            "Failed to resolve latest Huntarr version: %s",
                            latest_error,
                        )
                success, error = downloader.download_release_version(
                    process_name=instance.get("process_name"),
                    key="huntarr",
                    repo_owner=instance.get("repo_owner"),
                    repo_name=instance.get("repo_name"),
                    release_version=release_version,
                    target_dir=instance_config_dir,
                    exclude_dirs=exclude_dirs,
                )
                if not success:
                    return False, f"Failed to download Huntarr release: {error}"
                versions.version_write(
                    instance.get("process_name"),
                    key="huntarr",
                    version_path=os.path.join(instance_config_dir, "version.txt"),
                    version=version_to_write,
                )

            _chown_recursive_if_needed(
                instance_config_dir,
                CONFIG_MANAGER.get("puid"),
                CONFIG_MANAGER.get("pgid"),
            )

        if instance.get("platforms") and not configure_only:
            venv_marker = os.path.join(instance_config_dir, "venv", "bin", "python")
            if needs_download or not os.path.isfile(venv_marker):
                success, error = setup_environment(
                    process_handler,
                    "huntarr",
                    instance.get("platforms"),
                    instance_config_dir,
                )
                if not success:
                    return (
                        False,
                        f"Failed to set up environment for Huntarr instance {instance_name}: {error}",
                    )

        if not install_only and not instance.get("command"):
            instance["command"] = [
                os.path.join(instance_config_dir, "venv", "bin", "python"),
                "main.py",
            ]
            CONFIG_MANAGER.save_config(instance.get("process_name"))

        if not install_only:
            _patch_huntarr_database_paths(instance_config_dir)
            _patch_huntarr_backup_paths(instance_config_dir)

    if not install_only:
        try:
            from utils.huntarr_settings import patch_huntarr_config

            ok, err = patch_huntarr_config()
            if not ok and err:
                logger.warning("Huntarr config sync failed: %s", err)
        except Exception as exc:
            logger.warning("Huntarr config sync skipped: %s", exc)

    logger.info("Huntarr setup complete.")
    return True, None


def _patch_huntarr_database_paths(instance_config_dir: str) -> None:
    db_path = os.path.join(
        instance_config_dir, "src", "primary", "utils", "database.py"
    )
    if not os.path.isfile(db_path):
        logger.debug("Huntarr database.py not found at %s", db_path)
        return

    try:
        with open(db_path, "r") as handle:
            content = handle.read()
    except Exception as exc:
        logger.warning("Failed reading Huntarr database.py: %s", exc)
        return

    target_line = '        config_dir = Path("/config")\n'
    replacement_line = (
        '        config_dir = Path(os.environ.get("HUNTARR_CONFIG_DIR") or "/config")\n'
    )

    if target_line not in content:
        logger.debug("Huntarr database.py patch not needed or already applied.")
        return

    updated = content.replace(target_line, replacement_line, 1)
    try:
        with open(db_path, "w") as handle:
            handle.write(updated)
    except Exception as exc:
        logger.warning("Failed writing Huntarr database.py patch: %s", exc)


def _patch_huntarr_backup_paths(instance_config_dir: str) -> None:
    backup_path = os.path.join(instance_config_dir, "src", "routes", "backup_routes.py")
    if not os.path.isfile(backup_path):
        logger.debug("Huntarr backup_routes.py not found at %s", backup_path)
        return

    try:
        with open(backup_path, "r") as handle:
            content = handle.read()
    except Exception as exc:
        logger.warning("Failed reading Huntarr backup_routes.py: %s", exc)
        return

    target_line = '        config_dir = Path("/config")\n'
    replacement_line = (
        '        config_dir = Path(os.environ.get("HUNTARR_CONFIG_DIR") or "/config")\n'
    )

    if target_line not in content:
        logger.debug("Huntarr backup_routes.py patch not needed or already applied.")
        return

    updated = content.replace(target_line, replacement_line, 1)
    try:
        with open(backup_path, "w") as handle:
            handle.write(updated)
    except Exception as exc:
        logger.warning("Failed writing Huntarr backup_routes.py patch: %s", exc)


def setup_jellyfin(install_only: bool = False, configure_only: bool = False):
    config = CONFIG_MANAGER.get("jellyfin")

    if not config or not config.get("enabled"):
        logger.info("Jellyfin is disabled. Skipping setup.")
        return True, None

    def get_jellyfin_package_version():
        try:
            result = subprocess.run(
                ["dpkg-query", "-W", "-f=${Version}", "jellyfin"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except Exception:
            return None

    if install_only and configure_only:
        return False, "Invalid Jellyfin setup phase."

    jellyfin_service_path = "/usr/lib/jellyfin/bin/jellyfin"
    pinned_version = config.get("pinned_version")
    if not os.path.exists(jellyfin_service_path):
        logger.warning("Jellyfin service not found. Installing Jellyfin...")
        from utils.jellyfin import JellyfinInstaller

        installer = JellyfinInstaller()
        success, error = installer.install_jellyfin_server(version=pinned_version)
        if not success:
            return False, error
    elif pinned_version:
        current_version = get_jellyfin_package_version()
        if not current_version:
            logger.warning("Failed to read Jellyfin package version for pin check.")
        elif current_version != pinned_version:
            logger.info(
                f"Jellyfin pinned to {pinned_version}; installing over {current_version}."
            )
            from utils.jellyfin import JellyfinInstaller

            installer = JellyfinInstaller()
            success, error = installer.install_jellyfin_server(version=pinned_version)
            if not success:
                return False, error

    if install_only:
        return True, None

    if configure_only and not os.path.exists(jellyfin_service_path):
        return False, f"Jellyfin not installed at {jellyfin_service_path}."

    os.makedirs(config["config_dir"], exist_ok=True)
    chown_recursive(
        config["config_dir"], CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
    )
    sub_directories = [
        "data",
        "config",
        "cache",
        "log",
    ]
    for sub_dir in sub_directories:
        dir_path = os.path.join(config["config_dir"], sub_dir)
        if not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)
            chown_recursive(
                dir_path, CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
            )
        logger.info("Setting up Jellyfin Media Server environment...")
        config["command"] = [
            "/usr/lib/jellyfin/bin/jellyfin",
            "--datadir",
            os.path.join(config["config_dir"], "data"),
            "--configdir",
            os.path.join(config["config_dir"], "config"),
            "--cachedir",
            os.path.join(config["config_dir"], "cache"),
            "--logdir",
            os.path.join(config["config_dir"], "log"),
        ]
        try:
            from utils.jellyfin_settings import patch_jellyfin_config

            patch_jellyfin_config(config.get("port"))
        except Exception as e:
            logger.warning(f"Failed to patch Jellyfin system.xml port: {e}")
        return True, None


def setup_emby(install_only: bool = False, configure_only: bool = False):
    config = CONFIG_MANAGER.get("emby")
    if not config:
        return False, "Configuration for Emby not found."

    try:
        if install_only and configure_only:
            return False, "Invalid Emby setup phase."
        emby_config_dir = config.get("config_dir") or "/emby"
        emby_config_file = config.get("config_file")

        if not os.path.exists(emby_config_dir):
            logger.debug(f"Creating Emby config directory at {emby_config_dir}")
            os.makedirs(emby_config_dir, exist_ok=True)
        chown_single(emby_config_dir, user_id, group_id)

        candidate_bins = [
            "/opt/emby-server/system/EmbyServer",
            "/opt/emby-server/system/EmbyServer.dll",
            "/usr/lib/emby-server/bin/EmbyServer",
            "/opt/emby-server/bin/EmbyServer",
        ]
        emby_bin = next((p for p in candidate_bins if os.path.exists(p)), None)
        target_release = (
            config.get("release_version")
            if config.get("release_version_enabled") and config.get("release_version")
            else None
        )

        def install_emby(release):
            target_dir = "/tmp/emby_download"
            success, error = downloader.download_release_version(
                process_name=config.get("process_name"),
                key="emby",
                repo_owner=config.get("repo_owner"),
                repo_name=config.get("repo_name"),
                release_version=release,
                target_dir=target_dir,
                zip_folder_name=None,
            )
            if not success:
                return False, f"Failed to download Emby: {error}", None

            deb_path = None
            deb_candidates = [
                name
                for name in os.listdir(target_dir)
                if name.startswith("emby-server-deb_") and name.endswith(".deb")
            ]
            if release:
                release_prefix = f"emby-server-deb_{release}_"
                release_matches = [
                    name for name in deb_candidates if name.startswith(release_prefix)
                ]
                if release_matches:
                    deb_path = os.path.join(target_dir, sorted(release_matches)[0])
            if not deb_path and deb_candidates:
                deb_path = os.path.join(target_dir, sorted(deb_candidates)[0])
            if not deb_path:
                return (
                    False,
                    "Downloaded release does not contain an Emby .deb asset.",
                    None,
                )

            try:
                for name in deb_candidates:
                    candidate_path = os.path.join(target_dir, name)
                    if candidate_path != deb_path:
                        os.remove(candidate_path)
            except Exception:
                pass

            logger.info(f"Extracting Emby from {deb_path}")
            subprocess.run(["dpkg-deb", "-x", deb_path, "/"], check=True)
            for unit in (
                "/usr/lib/systemd/system/emby-server.service",
                "/lib/systemd/system/emby-server.service",
            ):
                if os.path.exists(unit):
                    try:
                        os.remove(unit)
                    except Exception:
                        pass
            emby_bin = next((p for p in candidate_bins if os.path.exists(p)), None)
            if not emby_bin:
                return (
                    False,
                    "Emby installed but binary not found in expected locations.",
                    None,
                )

            try:
                version_path = os.path.join(emby_config_dir, "version.txt")
                versions.version_write(
                    config.get("process_name", "Emby Media Server"),
                    "emby",
                    version_path=version_path,
                    version=release,
                )
                logger.debug(f"Emby version {release} written to {version_path}")
            except Exception:
                pass
            return True, None, emby_bin

        if not emby_bin:
            logger.warning("Emby service not found. Installing Emby…")
            if target_release:
                release = target_release
            else:
                release, error = downloader.get_latest_release(
                    repo_owner=config.get("repo_owner"),
                    repo_name=config.get("repo_name"),
                )
                if not release:
                    return False, f"Failed to get latest release: {error}"
            success, error, emby_bin = install_emby(release)
            if not success:
                return False, error
        elif target_release:
            current_version, error = versions.version_check(
                config.get("process_name", "Emby Media Server"), None, "emby"
            )
            if not current_version or current_version != target_release:
                logger.info(
                    f"Emby pinned to {target_release}; installing over {current_version or 'unknown'}."
                )
                success, error, emby_bin = install_emby(target_release)
                if not success:
                    return False, error
        if install_only:
            return True, None

        if configure_only and not emby_bin:
            return False, "Emby binary not installed."

        if emby_bin.endswith(".dll"):
            cmd = ["dotnet", emby_bin]
        else:
            cmd = [emby_bin]
        logger.info("Setting up Emby Server runtime...")
        use_system_ffmpeg = config.get("use_system_ffmpeg", True)
        if use_system_ffmpeg:

            def relink_binary(link_path, target_path, label):
                if not os.path.exists(target_path):
                    logger.warning(
                        f"System {label} not found at {target_path}; skipping relink."
                    )
                    return
                if os.path.islink(link_path) and os.readlink(link_path) == target_path:
                    logger.debug(f"Emby {label} already linked to system {label}.")
                    return
                if os.path.lexists(link_path):
                    backup_path = f"{link_path}.bak"
                    if os.path.exists(backup_path):
                        os.remove(backup_path)
                    shutil.move(link_path, backup_path)
                os.makedirs(os.path.dirname(link_path), exist_ok=True)
                os.symlink(target_path, link_path)
                logger.debug(f"Linked Emby {label} to system {label}.")

            relink_binary(
                "/opt/emby-server/bin/emby-ffmpeg", "/usr/bin/ffmpeg", "ffmpeg"
            )
            relink_binary("/opt/emby-server/bin/ffprobe", "/usr/bin/ffprobe", "ffprobe")
        config["command"] = cmd + [
            "-programdata",
            emby_config_dir,
            "-ffdetect",
            "/opt/emby-server/bin/emby-ffdetect",
            "-ffmpeg",
            "/opt/emby-server/bin/emby-ffmpeg",
            "-ffprobe",
            "/opt/emby-server/bin/ffprobe",
        ]
        logger.debug(f"Emby command set to: {config['command']}")
        config["env"] = config.get("env") or {}
        try:
            from utils.emby_settings import patch_emby_config

            patch_emby_config(config.get("port"))
        except Exception as e:
            logger.warning(f"Failed to patch Emby system.xml port: {e}")

        return True, None

    except Exception as e:
        return False, f"Error during Emby setup: {e}"


def zurg_setup(install_only: bool = False, configure_only: bool = False):
    config = CONFIG_MANAGER.get("zurg")
    if not config:
        return False, "Configuration for Zurg not found."

    logger.info("Starting Zurg setup...")

    try:
        if install_only and configure_only:
            return False, "Invalid Zurg setup phase."

        def setup_zurg_instance(instance, key_type):
            try:
                instance_config_dir = instance["config_dir"]
                if not os.path.exists(instance_config_dir):
                    logger.debug(
                        f"Creating Zurg instance {key_type} directory: {instance_config_dir}"
                    )
                    os.makedirs(instance_config_dir, exist_ok=True)
                    chown_recursive(
                        instance_config_dir,
                        CONFIG_MANAGER.get("puid"),
                        CONFIG_MANAGER.get("pgid"),
                    )
                instance_zurg_binaries = os.path.join(instance_config_dir, "zurg")
                instance_config_file = os.path.join(instance_config_dir, "config.yml")
                instance_plex_update_file = os.path.join(
                    instance_config_dir, "plex_update.sh"
                )
                if install_only:
                    if not os.path.exists(instance_zurg_binaries):
                        logger.debug(
                            "Zurg binary not found at %s. Downloading...",
                            instance_zurg_binaries,
                        )
                        release_version = (
                            instance.get("release_version")
                            if instance.get("release_version_enabled")
                            else "latest"
                        )
                        success, error = downloader.download_release_version(
                            process_name=instance["process_name"],
                            key="zurg",
                            repo_owner=instance["repo_owner"],
                            repo_name=instance["repo_name"],
                            release_version=release_version,
                            target_dir=instance_config_dir,
                            zip_folder_name=None,
                            exclude_dirs=instance.get("exclude_dirs", []),
                        )
                        if not success:
                            return False, f"Failed to download Zurg: {error}"
                        downloader.set_permissions(instance_zurg_binaries, 0o755)
                    return True, None

                if os.path.exists("/config/zurg"):
                    logger.debug(
                        f"Copying Zurg binary from override to {instance_zurg_binaries}"
                    )
                    shutil.copy("/config/zurg", instance_zurg_binaries)
                    chown_recursive(
                        instance_zurg_binaries,
                        CONFIG_MANAGER.get("puid"),
                        CONFIG_MANAGER.get("pgid"),
                    )

                if os.path.exists("/config/config.yml"):
                    logger.debug(
                        f"Copying config.yml from override to {instance_config_file}"
                    )
                    shutil.copy("/config/config.yml", instance_config_file)
                    chown_recursive(
                        instance_config_file,
                        CONFIG_MANAGER.get("puid"),
                        CONFIG_MANAGER.get("pgid"),
                    )

                elif not os.path.exists(instance_config_file):
                    logger.debug(
                        f"Copying config.yml from base to {instance_config_file}"
                    )
                    shutil.copy("/zurg/config.yml", instance_config_file)
                    chown_recursive(
                        instance_config_file,
                        CONFIG_MANAGER.get("puid"),
                        CONFIG_MANAGER.get("pgid"),
                    )

                if not os.path.exists(instance_plex_update_file):
                    shutil.copy("/zurg/plex_update.sh", instance_plex_update_file)
                    chown_recursive(
                        instance_plex_update_file,
                        CONFIG_MANAGER.get("puid"),
                        CONFIG_MANAGER.get("pgid"),
                    )
                config_dir_stat = os.stat(instance_config_dir)
                if config_dir_stat.st_uid != CONFIG_MANAGER.get(
                    "puid"
                ) or config_dir_stat.st_gid != CONFIG_MANAGER.get("pgid"):
                    logger.debug(
                        f"Changing ownership of {instance_config_dir} to {CONFIG_MANAGER.get('puid')}:{CONFIG_MANAGER.get('pgid')}"
                    )
                    os.chown(
                        instance_config_dir,
                        CONFIG_MANAGER.get("puid"),
                        CONFIG_MANAGER.get("pgid"),
                    )

                instance_user = instance["user"]
                instance_password = instance["password"]
                instance_port = instance["port"]
                logger.debug(f"Initial port from config: {instance_port}")
                if not instance_port:
                    instance_port = random.randint(9001, 9999)
                    logger.debug(f"Assigned random port: {instance_port}")
                    instance["port"] = instance_port

                update_port(instance_config_file, instance_port)

                token = instance["api_key"]
                if token:
                    update_token(instance_config_file, token)
                else:
                    return False, f"API key not found for Zurg instance {key_type}"

                update_creds(instance_config_file, instance_user, instance_password)

                logger.info(
                    f"Zurg instance '{key_type}' configured with port {instance_port}."
                )
                if not os.path.exists(instance_zurg_binaries):
                    if configure_only:
                        return False, f"Zurg binary missing for {key_type}."
                    logger.debug(
                        f"Zurg binary not found at {instance_zurg_binaries}. Downloading..."
                    )
                    if instance.get("release_version_enabled"):
                        release_version = instance.get("release_version")
                    else:
                        release_version = "latest"
                    success, error = downloader.download_release_version(
                        process_name=instance["process_name"],
                        key="zurg",
                        repo_owner=instance["repo_owner"],
                        repo_name=instance["repo_name"],
                        release_version=release_version,
                        target_dir=instance_config_dir,
                        zip_folder_name=None,
                        exclude_dirs=instance.get("exclude_dirs", []),
                    )
                    if not success:
                        return False, f"Failed to download Zurg: {error}"
                    downloader.set_permissions(instance_zurg_binaries, 0o755)
                elif os.path.exists(instance_zurg_binaries) and not configure_only:
                    logger.debug(f"Zurg binary found at {instance_zurg_binaries}.")
                    if not instance.get("release_version_enabled"):
                        current_version, update_info = versions.compare_versions(
                            process_name=instance["process_name"],
                            repo_owner=instance["repo_owner"],
                            repo_name=instance["repo_name"],
                            instance_name=key_type,
                            key="zurg",
                        )
                        if current_version:
                            logger.info(
                                f"Zurg instance '{key_type}' Current version: {update_info.get('current_version', 'unknown')} (latest: {update_info.get('latest_version', 'unknown')})"
                            )
                            release_version = "latest"
                            success, error = downloader.download_release_version(
                                process_name=instance["process_name"],
                                key="zurg",
                                repo_owner=instance["repo_owner"],
                                repo_name=instance["repo_name"],
                                release_version=release_version,
                                target_dir=instance_config_dir,
                                zip_folder_name=None,
                                exclude_dirs=instance.get("exclude_dirs", []),
                            )
                            if not success:
                                return False, f"Failed to download Zurg: {error}"
                            downloader.set_permissions(instance_zurg_binaries, 0o755)
                logger.info(f"Zurg instance '{key_type}' setup complete.")
                return True, None

            except Exception as e:
                return False, f"Error setting up Zurg instance for {key_type}: {e}"

        for key_type, instance in config.get("instances", {}).items():
            if instance.get("enabled"):
                if not instance.get("api_key"):
                    logger.error(f"API key not found for Zurg instance {key_type}")
                    raise ValueError(f"API key not found for Zurg instance {key_type}")
                logger.info(f"Setting up enabled instance: {key_type}")
                success, error = setup_zurg_instance(instance, key_type)
                if not success:
                    return False, error

        logger.info("All enabled Zurg instances have been set up.")
        return True, None

    except Exception as e:
        return False, f"Error during Zurg setup: {e}"


def update_port(config_file_path, instance_port):
    logger.debug(f"Updating port in {config_file_path} to {instance_port}")
    with open(config_file_path, "r") as file:
        lines = file.readlines()
    with open(config_file_path, "w") as file:
        for line in lines:
            if line.strip().startswith("port:") or line.strip().startswith("# port:"):
                file.write(f"port: {instance_port}\n")
            else:
                file.write(line)


def update_token(config_file_path, token):
    logger.debug(f"Updating token in {config_file_path}")
    with open(config_file_path, "r") as file:
        lines = file.readlines()
    with open(config_file_path, "w") as file:
        for line in lines:
            if line.strip().startswith("token:"):
                file.write(f"token: {token}\n")
            else:
                file.write(line)


def update_creds(config_file_path, username, password):
    if username and password:
        logger.debug(f"Updating credentials in {config_file_path}")
        with open(config_file_path, "r") as file:
            lines = file.readlines()
        with open(config_file_path, "w") as file:
            for line in lines:
                if line.strip().startswith("username:") or line.strip().startswith(
                    "# username:"
                ):
                    file.write(f"username: {username}\n")
                elif line.strip().startswith("password:") or line.strip().startswith(
                    "# password:"
                ):
                    file.write(f"password: {password}\n")
                else:
                    file.write(line)
    else:
        logger.debug(f"Removing credentials in {config_file_path}")
        with open(config_file_path, "r") as file:
            lines = file.readlines()
        with open(config_file_path, "w") as file:
            for line in lines:
                if line.strip().startswith("username:") or line.strip().startswith(
                    "# username:"
                ):
                    file.write("# username: <username>\n")
                elif line.strip().startswith("password:") or line.strip().startswith(
                    "# password:"
                ):
                    file.write("# password: <password>\n")
                else:
                    file.write(line)


def get_port_from_config(config_file_path):
    try:
        with open(config_file_path, "r") as file:
            for line in file:
                if line.strip().startswith("port:"):
                    port = line.split(":")[1].strip()
                    return port
    except Exception as e:
        logger.error(f"Error reading port from config file: {e}")
    return "9999"


def obscure_password(password):
    try:
        result = subprocess.run(
            ["rclone", "obscure", password], check=True, stdout=subprocess.PIPE
        )
        return result.stdout.decode().strip()
    except subprocess.CalledProcessError as e:
        logger.error(f"Error obscuring password: {e}")
        return None


def looks_like_dotnet_password_hash(value: str) -> bool:
    if value.startswith("$2"):
        return True
    if value.startswith("AQAAAA"):
        return True
    try:
        decoded = base64.b64decode(value, validate=True)
    except Exception:
        return False
    return bool(decoded) and decoded[0] == 0x01


def is_mount_point(path):
    with open("/proc/mounts", "r") as mounts:
        for line in mounts:
            if path in line.split():
                return True
    return False


def ensure_directory(mount_dir, mount_name):
    full_path = os.path.join(mount_dir, mount_name)
    logger.debug(
        "Ensuring directory %s exists (mount_dir=%r mount_name=%r)",
        full_path,
        mount_dir,
        mount_name,
    )

    try:
        os.makedirs(mount_dir, exist_ok=True)
    except OSError as e:
        return False, f"Failed to create mount base directory {mount_dir}: {e}"

    if is_mount_point(full_path):
        logger.info(f"{full_path} is a mount point. Attempting to unmount...")
        try:
            subprocess.run(["umount", full_path], check=True)
            logger.info(f"Successfully unmounted {full_path}.")
            return full_path, None
        except subprocess.CalledProcessError as e:
            return False, f"Failed to unmount {full_path}: {e.stderr}"

    if os.path.exists(full_path) and not os.path.isdir(full_path):
        return False, f"{full_path} exists but is not a directory."

    try:
        os.makedirs(full_path, exist_ok=True)
    except OSError as e:
        parent = os.path.dirname(full_path)
        return (
            False,
            "Failed to create mount directory {}: {} (parent_exists={}, parent_isdir={})".format(
                full_path, e, os.path.exists(parent), os.path.isdir(parent)
            ),
        )
    logger.info(f"Directory {full_path} is ready.")
    return full_path, None


def fuse_config():
    fuse_conf_path = "/etc/fuse.conf"
    user_allow_other_line = "user_allow_other"
    logger.info("Starting Rclone setup...")

    try:
        with open(fuse_conf_path, "r") as f:
            fuse_conf_content = f.readlines()

        updated_content = []
        line_found = False
        for line in fuse_conf_content:
            stripped_line = line.strip()
            if stripped_line == f"#{user_allow_other_line}":
                updated_content.append(f"{user_allow_other_line}\n")
                line_found = True
                logger.debug(
                    f"Uncommented '{user_allow_other_line}' in {fuse_conf_path}"
                )
            elif stripped_line == user_allow_other_line:
                line_found = True
                updated_content.append(line)
            else:
                updated_content.append(line)

        if not line_found:
            updated_content.append(f"{user_allow_other_line}\n")
            logger.debug(f"Added '{user_allow_other_line}' to {fuse_conf_path}")

        with open(fuse_conf_path, "w") as f:
            f.writelines(updated_content)
            return True, None
    except FileNotFoundError:
        with open(fuse_conf_path, "w") as f:
            f.write(f"{user_allow_other_line}\n")
        logger.debug(f"Created {fuse_conf_path} and added '{user_allow_other_line}'")
        return True, None
    except PermissionError:
        return False, "Permission denied while accessing /etc/fuse.conf."


def rclone_setup():
    config = CONFIG_MANAGER.get("rclone")
    if not config:
        return False, "Configuration for Rclone not found."

    success, error = fuse_config()
    if not success:
        return False, error

    def load_existing_config(config_file):
        config_data = {}
        if os.path.exists(config_file):
            with open(config_file, "r") as f:
                lines = f.readlines()
            section = None
            for line in lines:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1]
                    config_data[section] = []
                elif section and line:
                    config_data[section].append(line)
        return config_data

    def write_config(config_file, config_data):
        with open(config_file, "w") as f:
            for section, lines in config_data.items():
                f.write(f"[{section}]\n")
                f.write("\n".join(lines) + "\n")

    def scrub_rclone_log_file_flag(instance):
        existing = instance.get("command", [])
        if isinstance(existing, str):
            existing = shlex.split(existing)
        if not isinstance(existing, list):
            return False

        filtered = []
        skip_next = False
        for item in existing:
            if skip_next:
                skip_next = False
                continue
            if item == "--log-file":
                skip_next = True
                continue
            if isinstance(item, str) and item.startswith("--log-file="):
                continue
            filtered.append(item)

        if filtered != existing:
            instance["command"] = filtered
            return True
        return False

    try:

        def setup_rclone_instance(instance_name, instance):
            if not instance.get("enabled", False):
                logger.debug(f"Skipping disabled Rclone instance: {instance_name}")
                return True, None

            process_name = instance.get("process_name")
            from utils.dependencies import get_api_state

            api_state = get_api_state()
            if scrub_rclone_log_file_flag(instance):
                try:
                    CONFIG_MANAGER.save_config(process_name)
                except Exception as e:
                    logger.warning(
                        "Failed to persist rclone log-file cleanup for %s: %s",
                        process_name,
                        e,
                    )
            if api_state.get_status(process_name) == "running":
                logger.info(f"{process_name} is already running. Skipping setup.")
                return True, None

            config_file = instance["config_file"]
            config_dir = instance["config_dir"]
            mount_name = instance["mount_name"]
            mount_dir = instance["mount_dir"]
            os.makedirs(config_dir, exist_ok=True)
            logger.info(f"Setting up Rclone instance: {instance_name}")

            config_data = load_existing_config(config_file)

            if instance.get("zurg_enabled") and instance.get("decypharr_enabled"):
                return (
                    False,
                    "Both Zurg and Decypharr cannot be enabled at the same time for Rclone.",
                )

            if instance.get("zurg_enabled") and not instance.get("decypharr_enabled"):
                zurg_instance = (
                    CONFIG_MANAGER.get("zurg", {})
                    .get("instances", {})
                    .get(instance_name, {})
                )
                zurg_user = zurg_instance.get("user", "")
                zurg_password = zurg_instance.get("password", "")
                zurg_config_file = instance["zurg_config_file"]
                url = f"http://localhost:{get_port_from_config(zurg_config_file)}/dav/"

                config_data[mount_name] = [
                    "type = webdav",
                    f"url = {url}",
                    "vendor = other",
                    "pacer_min_sleep = 0",
                ]

                if zurg_user and zurg_password:
                    obscured_password = obscure_password(zurg_password)
                    if obscured_password:
                        config_data[mount_name].extend(
                            [
                                f"user = {zurg_user}",
                                f"pass = {obscured_password}",
                            ]
                        )
                    auth = {"user": zurg_user, "password": zurg_password}
                    instance["wait_for_url"] = [{"url": url, "auth": auth}]
                else:
                    instance["wait_for_url"] = [{"url": url}]

            elif instance.get("decypharr_enabled") and not instance.get("zurg_enabled"):
                decypharr_config = CONFIG_MANAGER.get("decypharr", {})
                key_type = instance.get("key_type", "").lower()
                if key_type == "realdebrid":
                    url = f"http://localhost:{decypharr_config.get('port', 8282)}/webdav/realdebrid"
                elif key_type == "alldebrid":
                    url = f"http://localhost:{decypharr_config.get('port', 8282)}/webdav/alldebrid"
                elif key_type == "debrid link":
                    url = f"http://localhost:{decypharr_config.get('port', 8282)}/webdav/debridlink"
                elif key_type == "torbox":
                    url = f"http://localhost:{decypharr_config.get('port', 8282)}/webdav/torbox"
                elif key_type == "usenet":
                    url = f"http://localhost:{decypharr_config.get('port', 8282)}/webdav/usenet"
                else:
                    url = key_type

                config_data[mount_name] = [
                    "type = webdav",
                    f"url = {url}",
                    "vendor = other",
                    "pacer_min_sleep = 0",
                ]
                instance["wait_for_url"] = [{"url": url}]

            else:
                key_type = instance.get("key_type", "").lower()
                if key_type == "realdebrid":
                    obscured_password = obscure_password(instance["password"])
                    config_data[mount_name] = [
                        "type = webdav",
                        "url = https://dav.real-debrid.com/",
                        "vendor = other",
                        f"user = {instance['username']}",
                        f"pass = {obscured_password}",
                    ]
                elif key_type == "alldebrid":
                    obscured_password = obscure_password("eeeee")
                    config_data[mount_name] = [
                        "type = webdav",
                        "url = https://webdav.debrid.it/",
                        "vendor = other",
                        f"user = {instance['api_key']}",
                        f"pass = {obscured_password}",
                    ]
                elif key_type == "premiumize":
                    obscured_password = obscure_password(instance["api_key"])
                    config_data[mount_name] = [
                        "type = webdav",
                        "url = davs://webdav.premiumize.me",
                        "vendor = other",
                        f"user = {instance['customer_id']}",
                        f"pass = {obscured_password}",
                    ]
                elif key_type == "torbox":
                    obscured_password = obscure_password(instance["password"])
                    config_data[mount_name] = [
                        "type = webdav",
                        "url = https://webdav.torbox.app",
                        "vendor = rclone",
                        f"user = {instance['username']}",
                        f"pass = {obscured_password}",
                        "pacer_min_sleep = 15s",
                    ]
                elif key_type == "torbox-ftp":
                    obscured_password = obscure_password(instance["password"])
                    config_data[mount_name] = [
                        "type = ftp",
                        "host = ftp.torbox.app",
                        f"user = {instance['username']}",
                        f"pass = {obscured_password}",
                    ]
                elif key_type == "nzbdav":
                    from utils import nzbdav_db

                    nzbdav_cfg = CONFIG_MANAGER.get("nzbdav", {})
                    nzbdav_env = (
                        nzbdav_cfg.get("env", {})
                        if isinstance(nzbdav_cfg, dict)
                        else {}
                    )
                    frontend_port = nzbdav_cfg.get("frontend_port", 3000)
                    url = f"http://127.0.0.1:{frontend_port}/"
                    instance["zurg_enabled"] = False
                    instance["decypharr_enabled"] = False
                    instance["zurg_config_file"] = ""
                    webdav_user = "admin"
                    webdav_pass = ""
                    env_webdav_user = nzbdav_env.get("WEBDAV_USER") or os.getenv(
                        "WEBDAV_USER"
                    )
                    env_webdav_pass = nzbdav_env.get("WEBDAV_PASSWORD") or os.getenv(
                        "WEBDAV_PASSWORD"
                    )
                    try:
                        value = nzbdav_db.get_config_value("webdav.user")
                        if value:
                            webdav_user = value
                        value = nzbdav_db.get_config_value("webdav.pass")
                        if value:
                            webdav_pass = value
                    except FileNotFoundError as e:
                        logger.warning("NzbDAV db not found for rclone setup: %s", e)

                    if env_webdav_user:
                        webdav_user = env_webdav_user

                    config_data[mount_name] = [
                        "type = webdav",
                        f"url = {url}",
                        "vendor = other",
                        "pacer_min_sleep = 0",
                        f"user = {webdav_user}",
                    ]
                    if env_webdav_pass:
                        webdav_pass = env_webdav_pass
                    if webdav_pass:
                        if looks_like_dotnet_password_hash(webdav_pass):
                            logger.warning(
                                "NzbDAV webdav.pass is a hashed value; set WEBDAV_PASSWORD to configure rclone."
                            )
                        else:
                            obscured_password = obscure_password(webdav_pass)
                            if obscured_password:
                                config_data[mount_name].append(
                                    f"pass = {obscured_password}"
                                )
                    auth = (
                        {"user": webdav_user, "password": webdav_pass}
                        if webdav_pass
                        else None
                    )
                    wait_entry = {"url": url}
                    if auth:
                        wait_entry["auth"] = auth
                    instance["wait_for_url"] = [wait_entry]

            write_config(config_file, config_data)

            full_path, error = ensure_directory(mount_dir, mount_name)
            if error:
                return False, f"Failed to ensure mount directory: {error}"
            if os.path.exists(full_path):
                stat_info = os.stat(full_path)
                if stat_info.st_uid != user_id or stat_info.st_gid != group_id:
                    logger.debug(
                        f"Changing ownership of {full_path} to {user_id}:{group_id}"
                    )
                    os.chown(full_path, user_id, group_id)
                else:
                    logger.debug(f"Ownership of {full_path} is already correct.")
            os.makedirs(instance["cache_dir"], exist_ok=True)
            chown_recursive(instance["cache_dir"], user_id, group_id)

            def update_or_generate_command(instance):
                mount_name = instance["mount_name"]
                mount_dir = instance["mount_dir"]
                config_file = instance["config_file"]
                cache_dir = os.path.abspath(instance["cache_dir"])
                log_level = instance.get("log_level", "INFO").upper()

                base_cmd = [
                    "rclone",
                    "mount",
                    f"{mount_name}:",
                    f"{mount_dir}/{mount_name}",
                ]
                required_flags = {
                    "--config": config_file,
                    "--uid": str(user_id),
                    "--gid": str(group_id),
                    "--allow-other": None,
                    "--poll-interval": "0",
                    "--dir-cache-time": "10s",
                    "--allow-non-empty": None,
                    "--cache-dir": cache_dir,
                    "--log-level": log_level,
                }
                default_flags = {}
                if instance.get("key_type", "").lower() == "nzbdav":
                    default_flags.update(
                        {
                            "--vfs-cache-mode": "full",
                            "--buffer-size": "1024M",
                            "--dir-cache-time": "1s",
                            "--vfs-cache-max-size": "5G",
                            "--vfs-cache-max-age": "180m",
                            "--links": None,
                            "--use-cookies": None,
                        }
                    )
                if instance.get("decypharr_enabled"):
                    used_ports = set()
                    all_instances = CONFIG_MANAGER.get("rclone", {}).get(
                        "instances", {}
                    )

                    for other_name, other in all_instances.items():
                        if other is instance or not other.get("decypharr_enabled"):
                            continue
                        other_cmd = other.get("command", [])
                        for i, token in enumerate(other_cmd):
                            if token == "--rc-addr" and i + 1 < len(other_cmd):
                                port = other_cmd[i + 1]
                                if port.startswith(":") and port[1:].isdigit():
                                    used_ports.add(int(port[1:]))
                    rc_port = 5572
                    while rc_port in used_ports:
                        rc_port += 1

                    required_flags.update(
                        {
                            "--rc": None,
                            "--rc-addr": f":{rc_port}",
                            "--rc-no-auth": None,
                        }
                    )

                existing = instance.get("command", [])
                parsed_flags = {}
                i = 0
                while i < len(existing):
                    item = existing[i]
                    if item.startswith("--"):
                        if "=" in item:
                            flag, val = item.split("=", 1)
                            parsed_flags[flag] = val
                        elif i + 1 < len(existing) and not existing[i + 1].startswith(
                            "--"
                        ):
                            parsed_flags[item] = existing[i + 1]
                            i += 1
                        else:
                            parsed_flags[item] = None
                    i += 1
                for key, value in required_flags.items():
                    parsed_flags[key] = value
                for key, value in default_flags.items():
                    if key not in parsed_flags:
                        parsed_flags[key] = value

                final_cmd = base_cmd
                for key, value in parsed_flags.items():
                    if value is None:
                        final_cmd.append(key)
                    elif key in {"--rc-addr"}:
                        final_cmd.extend([key, value])
                    else:
                        final_cmd.append(f"{key}={value}")

                instance["command"] = final_cmd
                logger.debug(
                    f"Final rclone command for {instance['mount_name']}: {final_cmd}"
                )

            update_or_generate_command(instance)
            from utils.riven_settings import parse_config_keys

            try:
                parse_config_keys(CONFIG_MANAGER.config)
            except Exception as e:
                logger.warning(
                    "Failed to update riven config keys during rclone setup: %s", e
                )
            logger.info(f"Rclone instance '{instance_name}' has been set up.")
            return True, None

        for instance_name, instance in config.get("instances", {}).items():
            success, error = setup_rclone_instance(instance_name, instance)
            if not success:
                logger.error("Rclone setup failed for %s: %s", instance_name, error)
                return False, error

        logger.info("All Rclone instances have been set up.")
        return True, None

    except Exception as e:
        logger.exception("Error during Rclone setup")
        return False, f"Error during Rclone setup: {e}"


def setup_environment(
    process_handler,
    key,
    platforms,
    config_dir,
    platform_dirs=None,
    dotnet_options=None,
):
    try:
        platform_dirs = platform_dirs or {}
        dotnet_options = dotnet_options or {}
        use_list_dirs = isinstance(config_dir, (list, tuple, set))

        def resolve_platform_dirs(platform):
            if platform in platform_dirs:
                dirs = platform_dirs.get(platform)
                return list(dirs) if isinstance(dirs, (list, tuple, set)) else [dirs]
            if use_list_dirs:
                return list(config_dir)
            if isinstance(config_dir, str):
                frontend = os.path.join(config_dir, "frontend")
                backend = os.path.join(config_dir, "backend")
                if "pnpm" in platforms and "dotnet" in platforms:
                    if platform == "pnpm" and os.path.isdir(frontend):
                        return [frontend]
                    if platform == "dotnet" and os.path.isdir(backend):
                        return [backend]
                if platform == "pnpm" and os.path.isdir(frontend):
                    return [frontend]
                if platform == "dotnet" and os.path.isdir(backend):
                    return [backend]
            return [config_dir]

        for platform in platforms:
            config_dirs = resolve_platform_dirs(platform)

            for env_dir in config_dirs:
                logger.info(
                    f"Setting up environment for {key} in {env_dir} with {platforms}"
                )

                if platform == "python":
                    success, error = setup_python_environment(
                        process_handler, key, env_dir
                    )
                    if not success:
                        return False, error

                if platform == "pnpm":
                    success, error = setup_pnpm_environment(process_handler, env_dir)
                    if not success:
                        return False, error

                if platform == "dotnet":
                    success, error = setup_dotnet_environment(
                        process_handler,
                        key,
                        env_dir,
                        project_paths=dotnet_options.get("project_paths"),
                        output_dir=dotnet_options.get("output_dir"),
                        restore_project_path=dotnet_options.get("restore_project_path"),
                        env=dotnet_options.get("env"),
                    )
                    if not success:
                        return False, error

        logger.info("Environment setup complete")
        return True, None
    except Exception as e:
        return False, f"Environment setup failed: {e}"


def clear_directory(directory_path, exclude_dirs=None, retries=3, delay=2):

    if exclude_dirs is None:
        exclude_dirs = []

    venv_path = os.path.abspath(os.path.join(directory_path, "venv"))
    if os.path.exists(venv_path) and not any(
        os.path.abspath(ex) == venv_path for ex in exclude_dirs
    ):
        logger.debug(f"Adding venv path to exclude_dirs: {venv_path}")
        exclude_dirs.append(venv_path)

    exclude_dirs = {os.path.abspath(exclude_dir) for exclude_dir in exclude_dirs}
    directory_path = os.path.abspath(directory_path)
    logger.debug(f"Excluding directories: {exclude_dirs}")

    def should_exclude(path):
        path = os.path.abspath(path)
        return any(
            path == exclude or path.startswith(f"{exclude}{os.sep}")
            for exclude in exclude_dirs
        )

    def clear_contents(path):
        for item in os.listdir(path):
            item_path = os.path.abspath(os.path.join(path, item))
            if should_exclude(item_path):
                logger.debug(f"Skipping excluded path: {item_path}")
                continue
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    clear_contents(item_path)
                    os.rmdir(item_path)
            except OSError as e:
                raise

    if not os.path.exists(directory_path):
        return False, f"Directory {directory_path} does not exist"

    try:
        logger.debug(f"Clearing directory: {directory_path}")
        clear_contents(directory_path)
        return True, None
    except OSError as e:
        if e.errno == 39:
            return True, None
        else:
            return False, f"Failed to clear directory {directory_path}: {e}"


def copy_server_config(source, destination):
    try:
        if not os.path.exists(source):
            logger.debug(f"server.json not found at {source}, skipping copy.")
            return

        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(source, destination)
        logger.info(f"Copied server configuration to {destination}")
    except Exception as e:
        logger.error(f"Error copying server configuration: {e}")


def setup_python_environment(process_handler, key, config_dir):
    try:

        requirements_file = (
            os.path.join(config_dir, "requirements.txt")
            if os.path.exists(os.path.join(config_dir, "requirements.txt"))
            else None
        )

        if key == "cli_debrid":
            requirements_file = (
                os.path.join(config_dir, "requirements-linux.txt")
                if os.path.exists(os.path.join(config_dir, "requirements-linux.txt"))
                else None
            )

        poetry_install = True if key == "riven_backend" else False

        logger.info(f"Setting up Python environment in {config_dir}")

        venv_path = os.path.join(config_dir, "venv")
        cache_root = os.path.join(config_dir, ".cache")
        pip_cache = os.path.join(cache_root, "pip")
        poetry_cache = os.path.join(cache_root, "pypoetry")
        os.makedirs(pip_cache, exist_ok=True)
        os.makedirs(poetry_cache, exist_ok=True)
        chown_single(cache_root, user_id, group_id)
        chown_single(pip_cache, user_id, group_id)
        chown_single(poetry_cache, user_id, group_id)

        process_handler.start_process(
            "python_env_setup", config_dir, ["python", "-m", "venv", "venv"]
        )
        process_handler.wait("python_env_setup")

        if process_handler.returncode != 0:
            return (
                False,
                f"Error creating Python virtual environment: {process_handler.stderr}",
            )
        logger.debug(f"venv_path: {venv_path} for {key}")
        python_executable = os.path.abspath(f"{venv_path}/bin/python")
        poetry_executable = os.path.abspath(f"{venv_path}/bin/poetry")
        pip_executable = os.path.abspath(f"{venv_path}/bin/pip")
        base_env = os.environ.copy()
        base_env["PIP_CACHE_DIR"] = pip_cache

        if requirements_file is not None:
            install_cmd = f"{pip_executable} install -r {requirements_file}"
            logger.debug(f"Installing requirements from {requirements_file} for {key}")
            process_handler.start_process(
                "install_requirements",
                config_dir,
                ["/bin/bash", "-c", install_cmd],
                env=base_env,
            )
            process_handler.wait("install_requirements")

            if process_handler.returncode != 0:
                return False, f"Error installing requirements: {process_handler.stderr}"

            logger.info(f"Installed requirements from {requirements_file}")

        if poetry_install is True:
            logger.debug(f"Installing Poetry for {key}")
            env = base_env.copy()
            env["PATH"] = f"{venv_path}/bin:" + env["PATH"]
            env["POETRY_VIRTUALENVS_CREATE"] = "false"
            env["VIRTUAL_ENV"] = venv_path
            env["POETRY_CACHE_DIR"] = poetry_cache
            env["POETRY_CONFIG_DIR"] = os.path.join(cache_root, "pypoetry", "config")
            env["POETRY_VIRTUALENVS_PATH"] = os.path.join(
                cache_root, "pypoetry", "virtualenvs"
            )

            process_handler.start_process(
                "install_poetry",
                config_dir,
                [python_executable, "-m", "pip", "install", "poetry"],
                None,
                False,
                env=env,
            )
            process_handler.wait("install_poetry")

            if process_handler.returncode != 0:
                return False, f"Error installing Poetry: {process_handler.stderr}"

            success, error = _maybe_patch_riven_plexapi_dependency(
                process_handler, key, config_dir, poetry_executable, env
            )
            if not success:
                return False, error

            process_handler.start_process(
                "poetry_install",
                config_dir,
                [poetry_executable, "install", "--no-root", "--without", "dev"],
                None,
                False,
                env=env,
            )
            process_handler.wait("poetry_install")

            if process_handler.returncode != 0:
                return False, f"Error installing dependencies with Poetry"

            logger.info(f"Poetry environment setup complete at {venv_path}")

        logger.info(f"Python environment setup complete")
        return True, None

    except Exception as e:
        return False, f"Error during Python environment setup: {e}"


def setup_dotnet_environment(
    process_handler,
    key,
    config_dir,
    project_paths=None,
    output_dir=None,
    restore_project_path=None,
    env=None,
):
    try:
        logger.info(f"Setting up .NET environment in {config_dir}")
        env = (env or os.environ.copy()).copy()
        nuget_packages = os.path.join(config_dir, ".nuget", "packages")
        os.makedirs(nuget_packages, exist_ok=True)
        chown_single(os.path.join(config_dir, ".nuget"), user_id, group_id)
        chown_single(nuget_packages, user_id, group_id)
        env.setdefault("NUGET_PACKAGES", nuget_packages)
        restore_target = restore_project_path or config_dir
        process_handler.start_process(
            "dotnet_env_restore",
            config_dir,
            ["dotnet", "restore", restore_target, "/nodeReuse:false"],
            env=env,
        )
        process_handler.wait("dotnet_env_restore")
        if process_handler.returncode != 0:
            return False, f"Error running dotnet restore: {process_handler.stderr}"
        if project_paths is None:
            project_paths = []
            if key == "zilean":
                project_paths = [
                    os.path.join(config_dir, "src/Zilean.ApiService"),
                    os.path.join(config_dir, "src/Zilean.Scraper"),
                ]
        for project_path in project_paths:
            if os.path.exists(project_path):
                logger.info(f"Publishing .NET project {project_path}")
                output_path = output_dir or os.path.join(config_dir, "app")
                process_handler.start_process(
                    "dotnet_publish",
                    config_dir,
                    [
                        "dotnet",
                        "publish",
                        project_path,
                        "-c",
                        "Release",
                        "--no-restore",
                        "-o",
                        output_path,
                        "/nodeReuse:false",
                        "/p:UseSharedCompilation=false",
                    ],
                    env=env,
                )
                process_handler.wait("dotnet_publish")
                if process_handler.returncode != 0:
                    return (
                        False,
                        f"Error publishing .NET project {project_path}: {process_handler.stderr}",
                    )

        logger.info(f".NET environment setup complete")
        return True, None

    except Exception as e:
        return False, f"Error during .NET environment setup: {e}"


def vite_modifications(config_dir):
    try:
        vite_config_path = os.path.join(config_dir, "vite.config.ts")
        with open(vite_config_path, "r") as file:
            lines = file.readlines()
        build_section_exists = any("build:" in line for line in lines)
        if not build_section_exists:
            for i, line in enumerate(lines):
                if line.strip().startswith("export default defineConfig({"):
                    lines.insert(i + 1, "    build: {\n        minify: false\n    },\n")
                    break
        with open(vite_config_path, "w") as file:
            file.writelines(lines)
        logger.debug("vite.config.ts modified to disable minification")
        about_page_path = os.path.join(
            config_dir, "src", "routes", "settings", "about", "+page.server.ts"
        )
        with open(about_page_path, "r") as file:
            about_page_lines = file.readlines()
        for i, line in enumerate(about_page_lines):
            if "versionFilePath: string = '/riven/version.txt';" in line:
                about_page_lines[i] = line.replace(
                    "/riven/version.txt", "/riven/frontend/version.txt"
                )
                logger.debug(
                    f"Modified versionFilePath in +page.ts to point to /riven/frontend/version.txt"
                )
                break
        with open(about_page_path, "w") as file:
            file.writelines(about_page_lines)
        return True, None

    except Exception as e:
        return False, f"Error modifying vite.config.ts: {e}"


def setup_pnpm_environment(process_handler, config_dir):
    try:
        _chown_recursive_if_needed(config_dir, user_id, group_id)
        with open(os.path.join(config_dir, ".npmrc"), "w") as file:
            file.write(
                "store-dir=./.pnpm-store\n"
                "child-concurrency=1\n"
                "network-concurrency=1\n"
                "fetch-retries=10\n"
                "fetch-retry-factor=3\n"
                "fetch-retry-mintimeout=15000\n"
                "package-import-method=copy\n"
            )

        logger.info(f"Setting up pnpm environment in {config_dir}")
        env = os.environ.copy()
        env["HOME"] = config_dir
        env["npm_config_userconfig"] = os.path.join(config_dir, ".npmrc")
        env["npm_config_cache"] = os.path.join(config_dir, ".npm-cache")
        os.makedirs(env["npm_config_cache"], exist_ok=True)
        chown_single(env["npm_config_cache"], user_id, group_id)
        env.setdefault("PNPM_NETWORK_CONCURRENCY", "1")
        env.setdefault("PNPM_CHILD_CONCURRENCY", "1")
        env.setdefault("PNPM_FETCH_RETRIES", "10")

        def _parse_required_pnpm_major():
            package_json_path = os.path.join(config_dir, "package.json")
            if not os.path.isfile(package_json_path):
                return None
            try:
                import json

                with open(package_json_path, "r") as f:
                    package_data = json.load(f)
                engines = package_data.get("engines", {}) or {}
                pnpm_req = engines.get("pnpm")
                if not pnpm_req:
                    return None
                match = re.search(r"(\d+)", str(pnpm_req))
                return match.group(1) if match else None
            except Exception as e:
                logger.debug("Failed to parse pnpm engine requirement: %s", e)
                return None

        def _pnpm_major_from_version(version):
            if not version:
                return None
            match = re.match(r"(\d+)", version.strip())
            return match.group(1) if match else None

        def _ensure_pnpm_version(required_major):
            if not required_major:
                return
            current_version = None
            try:
                result = subprocess.run(
                    ["pnpm", "-v"], capture_output=True, text=True, env=env
                )
                if result.returncode == 0:
                    current_version = result.stdout.strip()
            except FileNotFoundError:
                current_version = None

            current_major = _pnpm_major_from_version(current_version)
            if current_major == required_major:
                return

            logger.warning(
                "pnpm version mismatch (required %s.x, found %s). Attempting to switch.",
                required_major,
                current_version or "missing",
            )

            if shutil.which("corepack"):
                process_handler.start_process(
                    "corepack_prepare",
                    config_dir,
                    ["corepack", "prepare", f"pnpm@{required_major}", "--activate"],
                    env=env,
                )
                process_handler.wait("corepack_prepare")
            else:
                logger.warning(
                    "corepack not available; pnpm version may remain incompatible."
                )

        required_major = _parse_required_pnpm_major()
        _ensure_pnpm_version(required_major)
        use_corepack_pnpm = required_major is not None

        def cleanup_pnpm_tmp():
            pnpm_root = os.path.join(config_dir, "node_modules", ".pnpm")
            if not os.path.isdir(pnpm_root):
                return
            for entry in os.listdir(pnpm_root):
                if "_tmp_" not in entry and entry != "_tmp":
                    continue
                path = os.path.join(pnpm_root, entry)
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                except OSError as e:
                    logger.debug("Failed to remove pnpm tmp path %s: %s", path, e)

        for attempt in range(5):
            pnpm_cmd = ["pnpm", "install"]
            if use_corepack_pnpm:
                pnpm_cmd = ["corepack", "pnpm", "install"]
            process_handler.start_process("pnpm_install", config_dir, pnpm_cmd, env=env)
            process_handler.wait("pnpm_install")
            if process_handler.returncode == 0:
                break
            combined_output = (process_handler.stdout or "") + (
                process_handler.stderr or ""
            )
            if "eagain" not in combined_output.lower():
                return False, f"Error during pnpm install: {process_handler.stderr}"
            logger.warning(
                "pnpm install hit EAGAIN. Cleaning temp files and retrying..."
            )
            cleanup_pnpm_tmp()
            time.sleep(2**attempt)
        else:
            return False, f"Error during pnpm install: {process_handler.stderr}"

        package_json_path = os.path.join(config_dir, "package.json")
        build_script = None
        scripts = {}
        if os.path.isfile(package_json_path):
            import json

            with open(package_json_path, "r") as f:
                package_data = json.load(f)
                scripts = package_data.get("scripts", {}) or {}
                build_script = scripts.get("build")

        if build_script:
            logger.info("Build script found. Running pnpm build...")
            if use_corepack_pnpm and "pnpm " in build_script:
                script_names = []
                for match in re.findall(r"pnpm(?:\s+run)?\s+([^\s&|;]+)", build_script):
                    if match not in script_names:
                        script_names.append(match)
                if not script_names and scripts:
                    for candidate in ("build:next", "build:server"):
                        if candidate in scripts:
                            script_names.append(candidate)
                if script_names:
                    for script_name in script_names:
                        logger.info("Running pnpm %s via corepack...", script_name)
                        process_handler.start_process(
                            "pnpm_build",
                            config_dir,
                            ["corepack", "pnpm", "run", script_name],
                            env=env,
                        )
                        process_handler.wait("pnpm_build")
                        if process_handler.returncode != 0:
                            return (
                                False,
                                f"Error during pnpm {script_name}: {process_handler.stderr}",
                            )
                else:
                    logger.warning(
                        "Build script references pnpm but no sub-scripts found; using pnpm run build."
                    )
                    process_handler.start_process(
                        "pnpm_build",
                        config_dir,
                        ["corepack", "pnpm", "run", "build"],
                        env=env,
                    )
                    process_handler.wait("pnpm_build")
                    if process_handler.returncode != 0:
                        return (
                            False,
                            f"Error during pnpm build: {process_handler.stderr}",
                        )
            else:
                pnpm_build_cmd = ["pnpm", "run", "build"]
                if use_corepack_pnpm:
                    pnpm_build_cmd = ["corepack", "pnpm", "run", "build"]
                process_handler.start_process(
                    "pnpm_build", config_dir, pnpm_build_cmd, env=env
                )
                process_handler.wait("pnpm_build")
                if process_handler.returncode != 0:
                    return False, f"Error during pnpm build: {process_handler.stderr}"
        else:
            logger.info(f"No build script found. Skipping pnpm build step.")

        logger.info(f"pnpm environment setup complete")
        return True, None

    except Exception as e:
        return False, f"Error during pnpm setup: {e}"
