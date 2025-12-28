from utils import postgres
from utils.config_loader import CONFIG_MANAGER
from utils.global_logger import logger
from utils.download import Downloader
from utils.versions import Versions
from utils.plex import PlexInstaller
from utils.user_management import chown_recursive, chown_single
import base64
import yaml, os, shutil, random, subprocess, re, glob, secrets, shlex, time

user_id = CONFIG_MANAGER.get("puid")
group_id = CONFIG_MANAGER.get("pgid")
downloader = Downloader()
versions = Versions()


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

    if key == "decypharr" and config.get("branch_enabled"):
        success, error = build_decypharr_dev(process_handler, config)
        if not success:
            return False, f"Failed to build Decypharr development environment: {error}"

    return True, None


def setup_project(process_handler, process_name):
    key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
    if not key:
        raise ValueError(f"Key for {process_name} not found in the configuration.")

    config = CONFIG_MANAGER.get_instance(instance_name, key)
    if not config:
        raise ValueError(f"Configuration for {process_name} not found.")

    if process_name in process_handler.setup_tracker and not key == "nzbdav":
        process_handler.logger.info(
            f"{process_name} is already set up. Skipping setup."
        )
        return True, None

    logger.info(f"Setting up {process_name}...")
    try:
        if config.get("release_version_enabled") and not config.get("auto_update"):
            repo_owner = config.get("repo_owner")
            repo_name = config.get("repo_name")
            nightly = "nightly" in config["release_version"].lower()
            prerelease = config.get("release_version").lower() == "prerelease"
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
                    f"Update needed for {process_name}: {update_info['latest_version']}, but using the requested version: {config['release_version']}"
                )
                success, error = setup_release_version(
                    process_handler, config, process_name, key
                )
                if not success:
                    return False, error
            else:
                logger.info(
                    f"No update needed for {process_name}: current version is {update_info['current_version']}, and requested version is: {config['release_version']}"
                )

        elif config.get("branch_enabled"):
            success, error = setup_branch_version(
                process_handler, config, process_name, key
            )
            if not success:
                return False, error

        if config.get("env_copy"):
            src, dest = config["env_copy"]["source"], config["env_copy"]["destination"]
            if os.path.exists(src):
                shutil.copy(src, dest)
                logger.info(f"Copied .env from {src} to {dest}")

        if key == "nzbdav":
            backend_port = str(config.get("backend_port", 8080))
            default_env = {
                "LOG_LEVEL": config.get("log_level", "INFO").upper(),
                "WEBDAV_PASSWORD": config.get("webdav_password", "1P@55w0rd"),
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

        if config.get("env"):
            for env_key, value in config["env"].items():
                if isinstance(value, str) and "{" in value and "}" in value:
                    if "$" in value:
                        continue

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
                        value = (
                            value.replace("{postgres_host}", postgres_host)
                            .replace("{postgres_port}", str(postgres_port))
                            .replace("{postgres_user}", postgres_user)
                            .replace("{postgres_password}", postgres_password)
                        )

                    for placeholder in config.keys():
                        placeholder_pattern = f"{{{placeholder}}}"
                        if placeholder_pattern in value:
                            value = value.replace(
                                placeholder_pattern, str(config[placeholder])
                            )

                    config["env"][env_key] = value

        if key == "dumb_frontend":
            success, error = dumb_frontend_setup()
            if not success:
                return False, error

        if key == "riven_frontend":
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
        if key == "riven_backend":
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
            success, error = zurg_setup()
            if not success:
                return False, error

        if key == "zilean":
            config_app_wwwroot_dir = os.path.join(
                config["config_dir"], "app", "wwwroot"
            )
            config_wwwroot_dir = os.path.join(config["config_dir"], "wwwroot")
            if not os.path.exists(config_wwwroot_dir):
                os.symlink(config_app_wwwroot_dir, config_wwwroot_dir)

        if key == "rclone":
            success, error = rclone_setup()
            if not success:
                return False, error

        if key == "postgres":
            success, error = postgres.postgres_setup(process_handler)
            if not success:
                return False, error

        if key == "plex":
            success, error = setup_plex()
            if not success:
                return False, error

        if key == "jellyfin":
            success, error = setup_jellyfin()
            if not success:
                return False, error

        if key == "emby":
            success, error = setup_emby()
            if not success:
                return False, error

        if key == "pgadmin":
            success, error = postgres.pgadmin_setup(process_handler)
            if not success:
                return False, error

        if key == "plex_debrid":
            success, error = plex_debrid_setup()
            if not success:
                return False, error

        if key == "phalanx_db":
            success, error = phalanx_setup(process_handler)
            if not success:
                return False, error

        if key == "decypharr":
            success, error = setup_decypharr()
            if not success:
                return False, error

        if key == "nzbdav":
            success, error = setup_nzbdav(process_handler)
            if not success:
                return False, error

        if key == "bazarr":
            success, error = setup_bazarr(process_handler)
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
            for instance_name, instance in (
                CONFIG_MANAGER.get(key, {}).get("instances", {}).items()
            ):
                if not instance.get("enabled"):
                    continue
                process_name = instance["process_name"]
                logger.debug(
                    f"Setting up {process_name} with instance name {instance_name} for instance {instance}..."
                )
                success, error = setup_arr_instance(
                    key, instance_name, instance, process_name
                )
                if not success:
                    return False, error

        process_handler.setup_tracker.add(process_name)
        logger.debug(f"Post Setup tracker: {process_handler.setup_tracker}")
        logger.info(f"{process_name} setup complete")
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
        chown_recursive(os.path.dirname(config_file), user_id, group_id)
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
        else:
            logger.debug(f"[{app_name}] Port already set to {port} in config.xml")
        if loglevel_elem is not None and loglevel_elem.text != loglevel:
            logger.info(
                f"[{app_name}] Updating log level in config.xml from {loglevel_elem.text} to {loglevel}"
            )
            loglevel_elem.text = loglevel
            tree.write(config_file)
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
    except Exception as e:
        logger.error(f"[{app_name}] Failed to update existing config.xml: {e}")


def setup_arr_instance(key, instance_name, instance, process_name):
    binary_path = f"/opt/{key}/{key.capitalize()}/{key.capitalize()}"
    pinned_version = instance.get("pinned_version")
    if not os.path.exists(binary_path):
        logger.warning(f"{key.capitalize()} binary not found. Installing...")
        from utils.arr import ArrInstaller

        installer = ArrInstaller(key, version=pinned_version or "4")
        success, error = installer.install()
        if not success:
            return False, error
    elif pinned_version:
        current_version, error = versions.version_check(
            process_name, instance_name, key
        )
        if not current_version:
            logger.warning(
                f"Failed to read {key.capitalize()} version for pin check: {error}"
            )
        elif current_version != pinned_version:
            logger.info(
                f"{key.capitalize()} pinned to {pinned_version}; installing over {current_version}."
            )
            from utils.arr import ArrInstaller

            installer = ArrInstaller(key, version=pinned_version or "4")
            success, error = installer.install()
            if not success:
                return False, error
    if not os.access(binary_path, os.X_OK):
        logger.warning(f"{binary_path} not executable. Fixing permissions...")
        os.chmod(binary_path, 0o755)
    config_dir = instance["config_dir"]
    os.makedirs(config_dir, exist_ok=True)
    chown_recursive(config_dir, CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid"))

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

    return True, None


def setup_bazarr(process_handler=None):
    config = CONFIG_MANAGER.get("bazarr", {})
    if not config:
        return False, "Bazarr configuration not found."
    try:

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
            if not os.path.exists(bazarr_py_path):
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


def setup_decypharr():
    config = CONFIG_MANAGER.get("decypharr")
    if not config:
        return False, "Configuration for Decypharr not found."

    logger.info("Starting Decypharr setup...")

    try:
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

        if not os.path.isfile(binary_path):
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
        if decypharr_embedded_rclone:
            success, error = fuse_config()
            if not success:
                return False, error
        if os.path.exists(decypharr_config_file):
            from utils.decypharr_settings import patch_decypharr_config

            patch_decypharr_config()

        return True, None
    except Exception as e:
        return False, f"Error during Decypharr setup: {e}"


def setup_nzbdav(process_handler):
    config = CONFIG_MANAGER.get("nzbdav")
    if not config:
        return False, "Configuration for NzbDAV not found."

    logger.info("Starting NzbDAV setup...")

    try:
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
        chown_recursive(config_path, user_id, group_id)

        backend_project_path, _ = _find_nzbdav_backend_project(
            nzbdav_config_dir, config
        )
        if not backend_project_path or not os.path.exists(backend_project_path):
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

        if build_needed:
            success, error = setup_nzbdav_build(process_handler, config)
            if not success:
                return False, error

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


def phalanx_setup(process_handler):
    config = CONFIG_MANAGER.get("phalanx_db")
    if not config:
        return False, "Configuration for Phalanx not found."

    logger.info("Starting Phalanx setup...")

    try:
        phalanx_config_dir = config.get("config_dir")
        phalanx_data_dir = os.path.join(phalanx_config_dir, "data")
        original_package_file = os.path.join(phalanx_config_dir, "package.json")
        platforms = config.get("platforms", [])

        if not os.path.isfile(original_package_file):
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


def setup_plex():
    config = CONFIG_MANAGER.get("plex")

    if not config or not config.get("enabled"):
        logger.info("Plex is disabled. Skipping setup.")
        return True, None

    def normalize_version(version):
        if not version:
            return version
        return version[1:] if version.startswith("v") else version

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

    os.makedirs(config["config_dir"], exist_ok=True)
    if os.stat(config["config_dir"]).st_uid != CONFIG_MANAGER.get("puid"):
        chown_recursive(
            config["config_dir"], CONFIG_MANAGER.get("puid"), CONFIG_MANAGER.get("pgid")
        )
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
    command = ["/usr/lib/plexmediaserver/Plex Media Server"]
    config["command"] = command
    return True, None


def setup_jellyfin():
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
    return True, None


def setup_emby():
    config = CONFIG_MANAGER.get("emby")
    if not config:
        return False, "Configuration for Emby not found."

    try:
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
        if emby_bin.endswith(".dll"):
            cmd = ["dotnet", emby_bin]
        else:
            cmd = [emby_bin]
        logger.info("Setting up Emby Server runtime...")
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

        return True, None

    except Exception as e:
        return False, f"Error during Emby setup: {e}"


def zurg_setup():
    config = CONFIG_MANAGER.get("zurg")
    if not config:
        return False, "Configuration for Zurg not found."

    logger.info("Starting Zurg setup...")

    try:

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
                instance_user = instance["user"]
                instance_password = instance["password"]
                instance_port = instance["port"]
                logger.debug(f"Initial port from config: {instance_port}")
                if not instance_port:
                    instance_port = random.randint(9001, 9999)
                    logger.debug(f"Assigned random port: {instance_port}")
                    instance["port"] = instance_port
                instance_zurg_binaries = os.path.join(instance_config_dir, "zurg")
                instance_config_file = os.path.join(instance_config_dir, "config.yml")
                instance_plex_update_file = os.path.join(
                    instance_config_dir, "plex_update.sh"
                )

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
                elif os.path.exists(instance_zurg_binaries):
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

    try:

        def setup_rclone_instance(instance_name, instance):
            if not instance.get("enabled", False):
                logger.debug(f"Skipping disabled Rclone instance: {instance_name}")
                return True, None

            process_name = instance.get("process_name")
            from utils.dependencies import get_api_state

            api_state = get_api_state()
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
                log_file = os.path.abspath(instance["log_file"])
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
                    "--log-file": log_file,
                    "--log-level": log_level,
                }
                if instance.get("key_type", "").lower() == "nzbdav":
                    required_flags.update(
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

        if requirements_file is not None:
            install_cmd = f"{pip_executable} install -r {requirements_file}"
            logger.debug(f"Installing requirements from {requirements_file} for {key}")
            process_handler.start_process(
                "install_requirements", config_dir, ["/bin/bash", "-c", install_cmd]
            )
            process_handler.wait("install_requirements")

            if process_handler.returncode != 0:
                return False, f"Error installing requirements: {process_handler.stderr}"

            logger.info(f"Installed requirements from {requirements_file}")

        if poetry_install is True:
            logger.debug(f"Installing Poetry for {key}")
            env = os.environ.copy()
            env["PATH"] = f"{venv_path}/bin:" + env["PATH"]
            env["POETRY_VIRTUALENVS_CREATE"] = "false"
            env["VIRTUAL_ENV"] = venv_path

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
        chown_recursive(config_dir, user_id, group_id)
        with open(os.path.join(config_dir, ".npmrc"), "w") as file:
            file.write("store-dir=./.pnpm-store\n")

        logger.info(f"Setting up pnpm environment in {config_dir}")
        env = os.environ.copy()
        env["HOME"] = config_dir
        env["npm_config_userconfig"] = os.path.join(config_dir, ".npmrc")
        env.setdefault("PNPM_NETWORK_CONCURRENCY", "4")
        env.setdefault("PNPM_CHILD_CONCURRENCY", "2")
        env.setdefault("PNPM_FETCH_RETRIES", "5")

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
            process_handler.start_process(
                "pnpm_install", config_dir, ["pnpm", "install"], env=env
            )
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
        build_script_exists = False
        if os.path.isfile(package_json_path):
            import json

            with open(package_json_path, "r") as f:
                package_data = json.load(f)
                scripts = package_data.get("scripts", {})
                build_script_exists = "build" in scripts

        if build_script_exists:
            logger.info(f"Build script found. Running pnpm build...")
            process_handler.start_process(
                "pnpm_build", config_dir, ["pnpm", "run", "build"], env=env
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


def setup_traefik(process_handler):
    """Configures and starts Traefik using ProcessHandler"""

    traefik_config = CONFIG_MANAGER.get("traefik")
    if not traefik_config or not traefik_config.get("enabled"):
        logger.info("Traefik is disabled. Skipping setup.")
        return True, None

    config_dir = traefik_config.get("config_dir", "/config/traefik")

    # Ensure config directory exists
    os.makedirs(config_dir, exist_ok=True)

    logger.info(f"Setting up Traefik configuration in {config_dir}")

    # Generate traefik.yml (static configuration)
    static_config = {
        "entryPoints": traefik_config.get("entrypoints", {}),
        "api": {"dashboard": True},
        "providers": {"file": {"directory": config_dir, "watch": True}},
    }

    static_config_path = os.path.join(config_dir, "traefik.yml")
    with open(static_config_path, "w") as file:
        yaml.dump(static_config, file, default_flow_style=False)

    logger.info(f"Generated Traefik static config: {static_config_path}")

    # Generate dynamic configuration for services
    dynamic_config = {"http": {"routers": {}, "services": {}, "middlewares": {}}}

    # Add middleware definitions
    if "middlewares" in traefik_config:
        dynamic_config["http"]["middlewares"] = traefik_config["middlewares"]

    # Add services and routers
    for service_name, service_info in traefik_config.get("services", {}).items():
        router_name = f"{service_name}_router"
        service_url = service_info.get("url")
        middlewares = service_info.get("middlewares", [])

        if not service_url:
            logger.warning(f"Skipping {service_name}, no URL defined")
            continue

        dynamic_config["http"]["services"][service_name] = {
            "loadBalancer": {"servers": [{"url": service_url}]}
        }

        dynamic_config["http"]["routers"][router_name] = {
            "rule": f"PathPrefix(`/{service_name}`)",
            "service": service_name,
            "entryPoints": ["web"],
            "middlewares": middlewares,
        }

    dynamic_config_path = os.path.join(config_dir, "dynamic_config.yml")
    with open(dynamic_config_path, "w") as file:
        yaml.dump(dynamic_config, file, default_flow_style=False)

    logger.info(f"Generated Traefik dynamic config: {dynamic_config_path}")

    return True, None


def start_traefik(process_handler, config_dir: str):
    """Ensures Traefik is running via ProcessHandler"""

    traefik_bin = "/usr/local/bin/traefik"  # Adjust path if needed

    # Check if Traefik is already running
    if process_handler.is_process_running("traefik"):
        logger.info("Traefik is already running, restarting it.")
        process_handler.stop_process("traefik")

    # Start Traefik with config directory
    process_handler.start_process(
        process_name="traefik",
        cmd=[traefik_bin, "--configFile", os.path.join(config_dir, "traefik.yml")],
        env={},
    )

    logger.info("Traefik has been started successfully.")
