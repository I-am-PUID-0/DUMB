from utils.config_loader import CONFIG_MANAGER as config
from utils.global_logger import logger
from utils.resource_limits import available_cpu_count
from utils.cli_debrid_runtime import cli_battery_is_managed, prepare_cli_debrid_runtime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import os
import time
import grp
import pwd
import subprocess
import shutil
import secrets
import stat

user_id = config.get("puid")
group_id = config.get("pgid")

NETWORK_FILESYSTEM_TYPES = {
    "9p",
    "ceph",
    "cifs",
    "davfs",
    "davfs2",
    "glusterfs",
    "lustre",
    "nfs",
    "nfs4",
    "smb3",
    "sshfs",
}
NETWORK_FILESYSTEM_WORKER_LIMIT = 16
PRIVATE_CONTROLLER_CONFIG_ENTRIES = frozenset(
    {".dumb-setup", "installation-backups", "backup-storage"}
)
CONTROLLER_OWNED_CONFIG_ENTRIES = (
    frozenset({"arr-postgres-migration"}) | PRIVATE_CONTROLLER_CONFIG_ENTRIES
)


def _controller_state_mount_id(descriptor):
    with open(f"/proc/self/fdinfo/{descriptor}", encoding="ascii") as handle:
        for line in handle.read(16384).splitlines():
            if line.startswith("mnt_id:"):
                return int(line.split(":", 1)[1])
    raise ValueError("Mount identity is unavailable.")


def _controller_state_stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _repair_private_controller_tree(
    directory, owner_uid, owner_gid, managed_uid, managed_gid
):
    """Repair only fixed private controller state after a complete safe preflight.

    Old startup ownership traversal could assign these trees to the configured
    service UID. That known owner is eligible for migration; links, mounts,
    foreign owners, shared permissions and malformed file types are not.
    Record bytes and the SetupStore validation contract remain unchanged.
    """
    root_fd = -1
    rows = []
    allowed_uids = {owner_uid, managed_uid}
    allowed_gids = {owner_gid, managed_gid}

    def inspect(descriptor, mount_id):
        info = os.fstat(descriptor)
        if (
            info.st_uid not in allowed_uids
            or info.st_gid not in allowed_gids
            or stat.S_IMODE(info.st_mode) & 0o7077
            or _controller_state_mount_id(descriptor) != mount_id
        ):
            raise ValueError("Unsafe owner, permissions or mount identity.")
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1 or info.st_size > 8 * 1024 * 1024:
                raise ValueError("Unsafe hard link or oversized state file.")
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("Unsupported state file type.")
        return info

    def walk(descriptor, relative, depth, mount_id):
        if depth > 64 or len(rows) >= 10000:
            raise ValueError("Private state exceeds the supported traversal bound.")
        info = inspect(descriptor, mount_id)
        rows.append((relative, _controller_state_stamp(info)))
        if not stat.S_ISDIR(info.st_mode):
            return
        before = _controller_state_stamp(info)
        with os.scandir(descriptor) as entries:
            for entry in entries:
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                child = os.open(entry.name, flags, dir_fd=descriptor)
                try:
                    walk(
                        child,
                        relative + (entry.name,),
                        depth + 1,
                        mount_id,
                    )
                finally:
                    os.close(child)
        if before != _controller_state_stamp(os.fstat(descriptor)):
            raise ValueError("Private state changed during validation.")

    def open_relative(relative, mount_id):
        current = os.dup(root_fd)
        try:
            for index, part in enumerate(relative):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
                if index < len(relative) - 1:
                    flags |= os.O_DIRECTORY
                child = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = child
                if _controller_state_mount_id(current) != mount_id:
                    raise ValueError("Private state mount changed.")
            return os.dup(current)
        finally:
            os.close(current)

    try:
        root_fd = os.open(
            directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        parent_fd = os.open(
            str(Path(directory).parent),
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            mount_id = _controller_state_mount_id(parent_fd)
            walk(root_fd, (), 0, mount_id)
        finally:
            os.close(parent_fd)
        # No ownership changes occur until every entry has passed preflight.
        for relative, expected in rows:
            descriptor = open_relative(relative, mount_id)
            try:
                info = inspect(descriptor, mount_id)
                if _controller_state_stamp(info) != expected:
                    raise ValueError("Private state changed before ownership repair.")
                if (info.st_uid, info.st_gid) != (owner_uid, owner_gid):
                    os.fchown(descriptor, owner_uid, owner_gid)
            finally:
                os.close(descriptor)
        return True, None
    except (OSError, ValueError) as error:
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        message = (
            f"Private controller state {Path(directory).name} requires review "
            f"(controller UID {owner_uid}, configured PUID {managed_uid}): {reason} "
            "Preserve existing records. Restore controller ownership only for "
            "reviewed private regular files, or recover a verified private backup; "
            "do not delete state or follow links."
        )
        logger.error("%s", message)
        return False, message
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def _available_cpu_count():
    return available_cpu_count()


def validate_managed_user_ids(puid, pgid):
    invalid = []
    for name, value in (("PUID", puid), ("PGID", pgid)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            invalid.append(f"{name}={value!r}")

    if invalid:
        raise ValueError(
            "PUID and PGID must be positive integers greater than 0; "
            "UID/GID 0 would run managed services with root privileges "
            f"(invalid: {', '.join(invalid)})."
        )


def _generate_user_password():
    return secrets.token_urlsafe(18)


def _hash_user_password(user_password):
    result = subprocess.run(
        ["openssl", "passwd", "-6", "-stdin"],
        input=user_password,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _set_user_password(username, hashed_password):
    subprocess.run(["usermod", "-p", hashed_password, username], check=True)


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


def _decode_mountinfo_path(value):
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _filesystem_type(path):
    resolved = os.path.realpath(path)
    best_match = None
    best_type = None
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as mountinfo:
            for line in mountinfo:
                left, separator, right = line.rstrip("\n").partition(" - ")
                if not separator:
                    continue
                left_fields = left.split()
                right_fields = right.split()
                if len(left_fields) < 5 or not right_fields:
                    continue
                mount_point = os.path.realpath(_decode_mountinfo_path(left_fields[4]))
                try:
                    inside_mount = (
                        os.path.commonpath([resolved, mount_point]) == mount_point
                    )
                except ValueError:
                    continue
                if inside_mount and (
                    best_match is None or len(mount_point) > len(best_match)
                ):
                    best_match = mount_point
                    best_type = right_fields[0].lower()
    except OSError:
        return None
    return best_type


def get_dynamic_workers(directory=None):
    available = _available_cpu_count()
    if directory is None:
        return available
    filesystem_type = _filesystem_type(directory)
    if filesystem_type is None or filesystem_type in NETWORK_FILESYSTEM_TYPES:
        return min(available, NETWORK_FILESYSTEM_WORKER_LIMIT)
    if filesystem_type.startswith("fuse."):
        return min(available, NETWORK_FILESYSTEM_WORKER_LIMIT)
    return available


def chown_recursive(directory, user_id, group_id):
    try:
        max_workers = get_dynamic_workers(directory)
        start_time = time.time()
        logger.debug(
            "Using %s workers for chown operation on %s",
            max_workers,
            _filesystem_type(directory) or "unknown filesystem",
        )
        submitted = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for root, dirs, files in os.walk(directory):
                for dir_name in dirs:
                    executor.submit(
                        chown_single, os.path.join(root, dir_name), user_id, group_id
                    )
                    submitted += 1
                for file_name in files:
                    executor.submit(
                        chown_single, os.path.join(root, file_name), user_id, group_id
                    )
                    submitted += 1
            executor.submit(chown_single, directory, user_id, group_id)
            submitted += 1
        end_time = time.time()
        logger.debug(
            "chown_recursive for %s checked %s entries in %.2f seconds",
            directory,
            submitted,
            end_time - start_time,
        )
        return True, None
    except Exception as e:
        return False, f"Error changing ownership of '{directory}': {e}"


def _repair_controller_owned_tree(directory, owner_uid, owner_gid):
    """Restore one fixed private controller tree without following links."""

    try:
        root_stat = os.lstat(directory)
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            return (
                False,
                f"Controller-owned path '{directory}' is not a real directory.",
            )
        root_device = root_stat.st_dev
        pending = [directory]
        checked = 0
        repaired = 0
        while pending:
            current = pending.pop()
            current_stat = os.lstat(current)
            if (
                stat.S_ISLNK(current_stat.st_mode)
                or current_stat.st_dev != root_device
                or (current != directory and os.path.ismount(current))
            ):
                return (
                    False,
                    f"Controller-owned tree '{directory}' contains an unsafe link or mount.",
                )
            if stat.S_ISDIR(current_stat.st_mode):
                with os.scandir(current) as entries:
                    pending.extend(entry.path for entry in entries)
            elif not stat.S_ISREG(current_stat.st_mode):
                return (
                    False,
                    f"Controller-owned tree '{directory}' contains an unsupported entry.",
                )
            if (current_stat.st_uid, current_stat.st_gid) != (owner_uid, owner_gid):
                os.chown(current, owner_uid, owner_gid, follow_symlinks=False)
                repaired += 1
            checked += 1
        logger.debug(
            "Controller ownership check for %s checked %s entries and repaired %s.",
            directory,
            checked,
            repaired,
        )
        return True, None
    except Exception as error:
        return False, f"Error repairing controller ownership of '{directory}': {error}"


def chown_startup_directory(
    directory,
    user_id,
    group_id,
    *,
    controller_owned_entries=(),
    controller_uid=None,
    controller_gid=None,
):
    """Repair mismatched top-level ownership without walking healthy subtrees."""
    try:
        os.makedirs(directory, exist_ok=True)
        chown_single(directory, user_id, group_id)
        controller_owned_entries = frozenset(controller_owned_entries or ())
        controller_uid = os.geteuid() if controller_uid is None else controller_uid
        controller_gid = os.getegid() if controller_gid is None else controller_gid
        repaired = []
        skipped = 0
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name in controller_owned_entries:
                    if entry.name in PRIVATE_CONTROLLER_CONFIG_ENTRIES:
                        success, error = _repair_private_controller_tree(
                            entry.path,
                            controller_uid,
                            controller_gid,
                            user_id,
                            group_id,
                        )
                    else:
                        success, error = _repair_controller_owned_tree(
                            entry.path, controller_uid, controller_gid
                        )
                    if not success:
                        return False, error
                    skipped += 1
                    continue
                try:
                    stat_info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat_info.st_uid == user_id and stat_info.st_gid == group_id:
                    skipped += 1
                    continue
                repaired.append(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    success, error = chown_recursive(entry.path, user_id, group_id)
                    if not success:
                        return False, error
                else:
                    chown_single(entry.path, user_id, group_id)
        logger.debug(
            "Startup ownership check for %s skipped %s healthy top-level trees and repaired %s.",
            directory,
            skipped,
            len(repaired),
        )
        return True, None
    except Exception as error:
        return False, f"Error checking startup ownership of '{directory}': {error}"


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


def _prepare_managed_cli_runtime():
    cli = config.get("cli_debrid", {}) or {}
    battery = config.get("cli_battery", {}) or {}
    if not isinstance(cli, dict) or not isinstance(battery, dict):
        return
    if not (
        cli.get("enabled")
        or (battery.get("enabled") and cli_battery_is_managed(cli, battery))
    ):
        return
    # Fresh/custom checkouts are installed later. Their mandatory post-extract
    # and prelaunch checks create the data alias once the runtime exists.
    if not Path(cli.get("config_dir") or "/cli_debrid").is_dir():
        return
    success, error = prepare_cli_debrid_runtime(
        cli, config.get("data_root", "/data"), user_id, group_id
    )
    if not success:
        raise RuntimeError(error)


def _major_migration_recovery_pending():
    from utils.infinidysk_migration_admission import infinidysk_major_recovery_pending

    return infinidysk_major_recovery_pending()


def migrate_symlinks():
    data_root = str(config.get("data_root")) or "/data"
    logger.debug(f"Data root for symlinks: {data_root}")
    try:
        if is_mount(data_root):
            _prepare_managed_cli_runtime()
            cleanup_broken_symlinks(data_root)

            symlink_map = [
                ("/zurg/RD", os.path.join(data_root, "zurg_RD")),
                ("/riven/backend/data", os.path.join(data_root, "riven")),
                ("/postgres_data", os.path.join(data_root, "postgres")),
                ("/pgadmin/data", os.path.join(data_root, "pgadmin")),
                ("/zilean/app/data", os.path.join(data_root, "zilean")),
                ("/phalanx_db/data", os.path.join(data_root, "phalanx_db")),
                ("/decypharr", os.path.join(data_root, "decypharr")),
                ("/plex", os.path.join(data_root, "plex")),
                ("/tautulli", os.path.join(data_root, "tautulli")),
                ("/bazarr", os.path.join(data_root, "bazarr")),
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
                (
                    "/traefik-proxy-admin",
                    os.path.join(data_root, "traefik_proxy_admin"),
                ),
                ("/cloudflared", os.path.join(data_root, "cloudflared")),
                ("/neutarr", os.path.join(data_root, "neutarr")),
                ("/profilarr", os.path.join(data_root, "profilarr")),
                ("/pulsarr", os.path.join(data_root, "pulsarr")),
                ("/maintainerr", os.path.join(data_root, "maintainerr")),
                ("/aiostreams", os.path.join(data_root, "aiostreams")),
                ("/audiobookshelf", os.path.join(data_root, "audiobookshelf")),
                ("/mediastorm", os.path.join(data_root, "mediastorm")),
                ("/altmount", os.path.join(data_root, "altmount")),
            ]

            legacy_nzbdav_data = os.path.join(data_root, "nzbdav")
            infinidysk_config = config.get("infinidysk") or {}
            configured_root = str(infinidysk_config.get("config_dir") or "")
            legacy_namespace = (
                config.uses_legacy_infinidysk_identity()
                or configured_root == "/nzbdav"
                or configured_root.startswith("/nzbdav/")
                or os.path.lexists("/nzbdav")
                or os.path.exists(legacy_nzbdav_data)
            )
            if (
                str(infinidysk_config.get("runtime_generation") or "1") == "2"
                or _major_migration_recovery_pending()
            ):
                # A low-space job may deliberately leave this legacy source
                # absent. Recreating it here would obstruct archive recovery.
                logger.debug(
                    "Preserving legacy InfiniDysk paths during native/recovery boot."
                )
            elif legacy_namespace:
                # Existing installations keep this root until a guarded full
                # namespace migration is available and explicitly selected.
                symlink_map.append(("/nzbdav", legacy_nzbdav_data))
            else:
                symlink_map.append(
                    ("/infinidysk", os.path.join(data_root, "infinidysk"))
                )

            for original_path, data_path in symlink_map:
                migrate_and_symlink(original_path, data_path)
        else:
            logger.warning(
                f"Data root {data_root} is not a mount. Skipping symlink migration."
            )
    except Exception as e:
        logger.error(f"Error during symlink migration: {e}")
        raise


def ensure_gpu_group_membership(username):
    """Grant the managed account existing GPU groups without changing devices."""
    if not os.path.isdir("/dev/dri"):
        return
    account = pwd.getpwnam(username)
    groups = []
    for name in ("render", "video"):
        try:
            group = grp.getgrnam(name)
        except KeyError:
            continue
        if account.pw_gid != group.gr_gid and username not in group.gr_mem:
            groups.append(name)
    if groups:
        subprocess.run(["usermod", "-aG", ",".join(groups), username], check=True)


def create_system_user(username="DUMB"):
    validate_managed_user_ids(user_id, group_id)

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
            ensure_gpu_group_membership(username)
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

        ensure_gpu_group_membership(username)
        user_password = _generate_user_password()
        hashed_password = _hash_user_password(user_password)
        _set_user_password(username, hashed_password)
        logger.info(f"Password set for user '{username}'. Stored securely in memory.")

        zurg_dir = "/zurg"
        log_dir = config.get("dumb").get("log_dir")
        config_dir = "/config"
        debrid_mount_base_dir = "/mnt/debrid"
        riven_dir = "/riven/backend/data"
        zilean_dir = "/zilean/app/data"

        rclone_instances = config.get("rclone", {}).get("instances", {})

        chown_start = time.time()
        os.chown(zurg_dir, user_id, group_id)
        os.makedirs(debrid_mount_base_dir, exist_ok=True)
        debrid_stat = os.stat(debrid_mount_base_dir)
        if debrid_stat.st_uid != user_id or debrid_stat.st_gid != group_id:
            logger.debug(
                "Changing ownership of %s to %s:%s",
                debrid_mount_base_dir,
                user_id,
                group_id,
            )
            os.chown(debrid_mount_base_dir, user_id, group_id)
        else:
            logger.debug(
                "Directory %s is already owned by %s:%s",
                debrid_mount_base_dir,
                user_id,
                group_id,
            )
        chown_recursive(log_dir, user_id, group_id)
        config_ownership_ok, config_ownership_error = chown_startup_directory(
            config_dir,
            user_id,
            group_id,
            controller_owned_entries=CONTROLLER_OWNED_CONFIG_ENTRIES,
            controller_uid=os.geteuid(),
            controller_gid=os.getegid(),
        )
        if not config_ownership_ok:
            logger.warning("%s", config_ownership_error)
        chown_recursive(riven_dir, user_id, group_id)
        chown_recursive(home_dir, user_id, group_id)
        chown_recursive(zilean_dir, user_id, group_id)
        _prepare_managed_cli_runtime()

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
