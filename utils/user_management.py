from utils.config_loader import CONFIG_MANAGER as config
from utils.global_logger import logger
from concurrent.futures import ThreadPoolExecutor
import multiprocessing, os, time, grp, pwd, subprocess


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
        # mnt_dir = config.get("riven_backend").get("symlink_library_path")
        log_dir = config.get("dumb").get("log_dir")
        config_dir = "/config"
        riven_dir = "/riven/backend/data"
        zilean_dir = "/zilean/app/data"
        plex_debrid_dir = "/plex_debrid/config"
        cli_debrid_dir = "/cli_debrid/data"

        rclone_instances = config.get("rclone", {}).get("instances", {})

        chown_start = time.time()
        # chown_recursive(zurg_dir, user_id, group_id)
        # os.chown(mnt_dir, user_id, group_id)
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
        end_time = time.time()
        logger.info(
            f"Total time to create system user '{username}' was {end_time - start_time:.2f} seconds"
        )

    except Exception as e:
        logger.error(f"Error creating system user '{username}': {e}")
        raise
