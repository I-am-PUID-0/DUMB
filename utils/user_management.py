from utils.config_loader import CONFIG_MANAGER as config
from utils.global_logger import logger
from concurrent.futures import ThreadPoolExecutor
import multiprocessing, os, time, grp, pwd, subprocess, shutil

user_id = config.get("puid")
group_id = config.get("pgid")


def chown_single(path, user_id, group_id):
    try:
        stat_info = os.stat(path)
        if stat_info.st_uid == user_id and stat_info.st_gid == group_id:
            return
        os.chown(path, user_id, group_id)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"Error changing ownership of '{path}': {e}")


def log_directory_size(directory):
    try:
        num_files = sum([len(files) for r, d, files in os.walk(directory)])
        logger.debug(f"Directory '{directory}' contains {num_files} files.")
    except Exception as e:
        logger.error(f"Error calculating size of directory '{directory}': {e}")


def get_dynamic_workers():
    return multiprocessing.cpu_count()


def chown_recursive(directory, user_id, group_id):
    try:
        max_workers = get_dynamic_workers()
        start_time = time.time()
        log_directory_size(directory)
        logger.debug(f"Using {max_workers} workers for chown operation")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for root, dirs, files in os.walk(directory):
                for dir_name in dirs:
                    executor.submit(
                        chown_single, os.path.join(root, dir_name), user_id, group_id
                    )
                for file_name in files:
                    executor.submit(
                        chown_single, os.path.join(root, file_name), user_id, group_id
                    )
            executor.submit(chown_single, directory, user_id, group_id)
        end_time = time.time()
        logger.debug(
            f"chown_recursive for {directory} took {end_time - start_time:.2f} seconds"
        )
        return True, None
    except Exception as e:
        return False, f"Error changing ownership of '{directory}': {e}"


def is_mount(path):
    return os.path.ismount(path)


def migrate_and_symlink(original_path, data_path):
    try:
        if not os.path.exists(data_path):
            os.makedirs(data_path, exist_ok=True)
            logger.debug(f"Created data path: {data_path}")
            chown_recursive(data_path, user_id, group_id)

        def empty_dir(path):
            return not os.path.exists(path) or (
                os.path.isdir(path) and not os.listdir(path)
            )

        if (
            os.path.exists(original_path)
            and not os.path.islink(original_path)
            and not empty_dir(original_path)
        ):
            if not os.listdir(data_path):
                logger.info(f"Migrating data from {original_path} → {data_path}")
                try:
                    shutil.copytree(
                        original_path, data_path, dirs_exist_ok=True, symlinks=True
                    )
                    chown_recursive(data_path, user_id, group_id)
                    original_size = sum(
                        os.path.getsize(os.path.join(root, file))
                        for root, _, files in os.walk(original_path)
                        for file in files
                    )
                    data_size = sum(
                        os.path.getsize(os.path.join(root, file))
                        for root, _, files in os.walk(data_path)
                        for file in files
                    )
                    if original_size != data_size:
                        raise Exception(
                            f"Data size mismatch: {original_size} bytes in original, {data_size} bytes in data path"
                        )
                    logger.debug(
                        f"Data migration successful: {original_path} (bytes: {original_size}) → {data_path} (bytes: {data_size})"
                    )
                except Exception as e:
                    logger.error(
                        f"Error copying data from {original_path} to {data_path}: {e}"
                    )
                    raise
            else:
                logger.debug(f"{data_path} already has content, skipping copy")

        if is_mount(original_path):
            logger.info(
                f"Cannot symlink {original_path} → {data_path} because it is a mount. Remove the mount in Docker Compose to complete migration."
            )
            return

        if os.path.exists(original_path) and not os.path.islink(original_path):
            shutil.rmtree(original_path)
            logger.debug(f"Removed original path: {original_path}")

        if not os.path.exists(original_path):
            os.makedirs(os.path.dirname(original_path), exist_ok=True)
            os.symlink(data_path, original_path)
            if (
                os.path.islink(original_path)
                and os.readlink(original_path) == data_path
            ):
                logger.debug(f"Created symlink: {original_path} → {data_path}")
            else:
                raise Exception(
                    f"Failed to create symlink: {original_path} → {data_path}"
                )

    except Exception as e:
        raise RuntimeError(
            f"Migration failed for {original_path} to {data_path}: {e}"
        ) from e


def cleanup_broken_symlinks(directory):
    if not os.path.exists(directory):
        return
    for item in os.listdir(directory):
        path = os.path.join(directory, item)
        if os.path.islink(path) and not os.path.exists(os.readlink(path)):
            try:
                os.unlink(path)
                logger.debug(f"Removed broken symlink: {path}")
            except Exception as e:
                logger.warning(f"Failed to remove broken symlink: {path}: {e}")


def migrate_symlinks():
    data_root = str(config.get("data_root")) or "/data"
    logger.debug(f"Data root for symlinks: {data_root}")
    try:
        if is_mount(data_root):
            cleanup_broken_symlinks(data_root)

            symlink_map = [
                ("/zurg/RD", os.path.join(data_root, "zurg_RD")),
                ("/riven/backend/data", os.path.join(data_root, "riven")),
                ("/postgres_data", os.path.join(data_root, "postgres")),
                ("/pgadmin/data", os.path.join(data_root, "pgadmin")),
                ("/zilean/app/data", os.path.join(data_root, "zilean")),
                ("/plex_debrid/config", os.path.join(data_root, "plex_debrid")),
                ("/cli_debrid/data", os.path.join(data_root, "cli_debrid")),
                ("/phalanx_db/data", os.path.join(data_root, "phalanx_db")),
                ("/decypharr", os.path.join(data_root, "decypharr")),
                ("/nzbdav", os.path.join(data_root, "nzbdav")),
                ("/plex", os.path.join(data_root, "plex")),
                ("/tautulli", os.path.join(data_root, "tautulli")),
                ("/seerr", os.path.join(data_root, "seerr")),
                ("/jellyfin", os.path.join(data_root, "jellyfin")),
                ("/emby", os.path.join(data_root, "emby")),
                ("/sonarr", os.path.join(data_root, "sonarr")),
                ("/radarr", os.path.join(data_root, "radarr")),
                ("/lidarr", os.path.join(data_root, "lidarr")),
                ("/prowlarr", os.path.join(data_root, "prowlarr")),
                ("/readarr", os.path.join(data_root, "readarr")),
                ("/whisparr", os.path.join(data_root, "whisparr")),
                ("/traefik", os.path.join(data_root, "traefik")),
                ("/huntarr", os.path.join(data_root, "huntarr")),
            ]

            for original_path, data_path in symlink_map:
                migrate_and_symlink(original_path, data_path)
        else:
            logger.warning(
                f"Data root {data_root} is not a mount. Skipping symlink migration."
            )
    except Exception as e:
        logger.error(f"Error during symlink migration: {e}")
        raise


def create_system_user(username="DUMB"):
    try:
        start_time = time.time()
        group_check_start = time.time()
        try:
            grp.getgrgid(group_id)
            logger.debug(f"Group with GID {group_id} already exists.")
        except KeyError:
            logger.info(f"Group with GID {group_id} does not exist. Creating group...")
            with open("/etc/group", "a") as group_file:
                group_file.write(f"{username}:x:{group_id}:\n")
        group_check_end = time.time()
        logger.debug(
            f"Group check/creation took {group_check_end - group_check_start:.2f} seconds"
        )

        user_check_start = time.time()
        try:
            pwd.getpwnam(username)
            logger.debug(f"User '{username}' with UID {user_id} already exists.")
            migrate_symlinks()
            return
        except KeyError:
            logger.info(f"User '{username}' does not exist. Creating user...")
        user_check_end = time.time()
        logger.debug(f"User check took {user_check_end - user_check_start:.2f} seconds")

        home_dir = f"/home/{username}"
        if not os.path.exists(home_dir):
            os.makedirs(home_dir)

        passwd_write_start = time.time()
        with open("/etc/passwd", "a") as passwd_file:
            passwd_file.write(
                f"{username}:x:{user_id}:{group_id}::/home/{username}:/bin/bash\n"
            )
        passwd_write_end = time.time()
        logger.debug(
            f"Writing to /etc/passwd took {passwd_write_end - passwd_write_start:.2f} seconds"
        )

        user_password = (
            subprocess.check_output("openssl rand -base64 12", shell=True)
            .decode()
            .strip()
        )
        hashed_password = (
            subprocess.check_output(f"openssl passwd -6 {user_password}", shell=True)
            .decode()
            .strip()
        )
        subprocess.run(
            f"usermod -p '{hashed_password}' {username}", shell=True, check=True
        )
        logger.info(f"Password set for user '{username}'. Stored securely in memory.")

        zurg_dir = "/zurg"
        log_dir = config.get("dumb").get("log_dir")
        config_dir = "/config"
        riven_dir = "/riven/backend/data"
        zilean_dir = "/zilean/app/data"
        plex_debrid_dir = "/plex_debrid/config"
        cli_debrid_dir = "/cli_debrid/data"

        rclone_instances = config.get("rclone", {}).get("instances", {})

        chown_start = time.time()
        os.chown(zurg_dir, user_id, group_id)
        chown_recursive(log_dir, user_id, group_id)
        chown_recursive(config_dir, user_id, group_id)
        chown_recursive(riven_dir, user_id, group_id)
        chown_recursive(home_dir, user_id, group_id)
        chown_recursive(zilean_dir, user_id, group_id)
        chown_recursive(plex_debrid_dir, user_id, group_id)
        chown_recursive(cli_debrid_dir, user_id, group_id)

        for instance_name, instance_config in rclone_instances.items():
            if instance_config.get("enabled", False):
                rclone_dir = instance_config.get("mount_dir")
                if rclone_dir and os.path.exists(rclone_dir):
                    stat_info = os.stat(rclone_dir)
                    if stat_info.st_uid == user_id and stat_info.st_gid == group_id:
                        logger.debug(
                            f"Directory {rclone_dir} is already owned by {user_id}:{group_id}"
                        )
                    else:
                        logger.debug(
                            f"Directory {rclone_dir} is not owned by {user_id}:{group_id}, changing ownership"
                        )
                        logger.debug(
                            f"Changing ownership of {rclone_dir} for {instance_name}"
                        )
                        chown_recursive(rclone_dir, user_id, group_id)
                else:
                    logger.warning(
                        f"Mount directory for {instance_name} does not exist or is not set: {rclone_dir}"
                    )

        chown_end = time.time()
        logger.debug(f"Chown operations took {chown_end - chown_start:.2f} seconds")

        migration_start = time.time()
        migrate_symlinks()
        migration_end = time.time()
        logger.debug(
            f"Migration of symlinks took {migration_end - migration_start:.2f} seconds"
        )

        end_time = time.time()
        logger.info(
            f"Total time to create system user '{username}' was {end_time - start_time:.2f} seconds"
        )

    except Exception as e:
        logger.error(f"Error creating system user '{username}': {e}")
        raise
