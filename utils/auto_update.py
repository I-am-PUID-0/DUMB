from utils.global_logger import logger
from utils.logger import format_time
from utils.versions import Versions
from utils.setup import setup_project, setup_release_version
from utils.plex import PlexInstaller
from utils.arr import ArrInstaller
from utils.jellyfin import JellyfinInstaller
from utils.config_loader import CONFIG_MANAGER
import threading, time, os, schedule, requests, subprocess


class Update:
    _scheduler_initialized = False
    _jobs = {}

    def __init__(self, process_handler):
        self.process_handler = process_handler
        self.logger = process_handler.logger
        self.updating = threading.Lock()

        if not Update._scheduler_initialized:
            self.scheduler = schedule.Scheduler()
            Update._scheduler_initialized = True
        else:
            self.scheduler = schedule.default_scheduler

    def update_schedule(self, process_name, config, key, instance_name):
        interval_minutes = int(self.auto_update_interval(process_name, config) * 60)
        self.logger.debug(
            f"Scheduling automatic update check every {interval_minutes} minutes for {process_name}"
        )

        if process_name not in Update._jobs:
            self.scheduler.every(interval_minutes).minutes.do(
                self.scheduled_update_check, process_name, config, key, instance_name
            )
            Update._jobs[process_name] = True
            self.logger.debug(
                f"Scheduled automatic update check for {process_name}, w/ key: {key}, and job ID: {id(self.scheduler.jobs[-1])}"
            )

        while not self.process_handler.shutting_down:
            self.scheduler.run_pending()
            time.sleep(1)

    def auto_update_interval(self, process_name, config):
        default_interval = 24
        try:
            interval = config.get("auto_update_interval", default_interval)
        except Exception as e:
            self.logger.error(
                f"Failed to retrieve auto_update_interval for {process_name}: {e}"
            )
            interval = default_interval

        return interval

    def auto_update(self, process_name, enable_update):
        key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
        config = CONFIG_MANAGER.get_instance(instance_name, key)
        if not config:
            return None, f"Configuration for {process_name} not found."

        if key in ["bazarr"]:
            enable_update = False
            self.logger.info(
                f"Automatic updates are not yet supported for {process_name} ({key})."
            )
        if enable_update:
            self.logger.info(
                f"Automatic updates set to {format_time(self.auto_update_interval(process_name, config))} for {process_name}"
            )
            self.schedule_thread = threading.Thread(
                target=self.update_schedule,
                args=(process_name, config, key, instance_name),
            )
            self.schedule_thread.start()

            return self.initial_update_check(process_name, config, key, instance_name)
        else:
            self.logger.info(f"Automatic update disabled for {process_name}")
            success, error = setup_project(self.process_handler, process_name)
            if not success:
                return None, error

            return self.start_process(process_name, config, key, instance_name)

    def initial_update_check(self, process_name, config, key, instance_name):
        with self.updating:
            self.logger.info(f"Performing initial update check for {process_name}")
            success, error = self.update_check(process_name, config, key, instance_name)
            if not success:
                if "No updates available" in error:
                    self.logger.info(error)
                    success, error = setup_project(self.process_handler, process_name)
                    if not success:
                        return None, f"Failed to set up {process_name}: {error}"

                    return self.start_process(process_name, config, key, instance_name)
                else:
                    return None, error

            return True, error

    def scheduled_update_check(self, process_name, config, key, instance_name):
        with self.updating:
            self.logger.info(f"Performing scheduled update check for {process_name}")
            success, error = self.update_check(process_name, config, key, instance_name)
            if not success:
                if "No updates available" in error:
                    self.logger.info(error)
                    # self.start_process(process_name, config, key, instance_name)
                else:
                    raise RuntimeError(error)

    def update_check(self, process_name, config, key, instance_name):
        if key == "plex":
            return self.update_check_plex(process_name, config, key, instance_name)
        if key == "jellyfin":
            pinned_version = config.get("pinned_version")
            if pinned_version:
                return self.update_check_pinned_version(
                    process_name,
                    config,
                    key,
                    instance_name,
                    pinned_version,
                )
            return self.update_check_jellyfin_latest(
                process_name, config, key, instance_name
            )
        if key == "emby":
            target_release = None
            if config.get("release_version_enabled"):
                target_release = config.get("release_version")
            if target_release:
                return self.update_check_pinned_version(
                    process_name, config, key, instance_name, target_release
                )
            return self.update_check_emby_latest(
                process_name, config, key, instance_name
            )
        if key in [
            "sonarr",
            "radarr",
            "lidarr",
            "prowlarr",
            "readarr",
            "whisparr",
            "whisparr-v3",
        ]:
            pinned_version = config.get("pinned_version")
            if pinned_version:
                return self.update_check_pinned_version(
                    process_name,
                    config,
                    key,
                    instance_name,
                    pinned_version,
                )
            return self.update_check_arr_latest(
                process_name, config, key, instance_name
            )

        if config.get("release_version_enabled"):
            release_value = (config.get("release_version") or "").lower()
            if "nightly" in release_value:
                nightly = True
                prerelease = False
                self.logger.info(f"Checking for nightly updates for {process_name}.")
            elif "prerelease" in release_value:
                nightly = False
                prerelease = True
                self.logger.info(f"Checking for prerelease updates for {process_name}.")
            else:
                nightly = False
                prerelease = False
                self.logger.info(f"Checking for stable updates for {process_name}.")
        else:
            nightly = False
            prerelease = False
            self.logger.info(f"Checking for stable updates for {process_name}.")

        versions = Versions()
        try:
            repo_owner = config["repo_owner"]
            repo_name = config["repo_name"]
            update_needed, update_info = versions.compare_versions(
                process_name,
                repo_owner,
                repo_name,
                instance_name,
                key,
                nightly=nightly,
                prerelease=prerelease,
            )

            if not update_needed:
                return False, f"{update_info.get('message')} for {process_name}."

            self.logger.info(
                f"Updating {process_name} from {update_info.get('current_version')} to {update_info.get('latest_version')}."
            )
            if process_name in self.process_handler.process_names:
                self.stop_process(process_name)
            with self.process_handler.setup_tracker_lock:
                if process_name in self.process_handler.setup_tracker:
                    self.process_handler.setup_tracker.remove(process_name)
            release_version = f"{update_info.get('latest_version')}"
            if not prerelease and not nightly:
                config["release_version"] = release_version
                self.logger.info(
                    f"Updating {process_name} config to {release_version}."
                )
            success, error = setup_release_version(
                self.process_handler, config, process_name, key
            )
            if not success:
                return (
                    False,
                    f"Failed to update {process_name} to {release_version}: {error}",
                )
            success, error = setup_project(self.process_handler, process_name)
            if not success:
                return (
                    False,
                    f"Failed to update {process_name} to {release_version}: {error}",
                )
            self.start_process(process_name, config, key, instance_name)
            return True, f"Updated {process_name} to {release_version}."

        except Exception as e:
            return False, f"Update check failed for {process_name}: {e}"

    def update_check_pinned_version(
        self, process_name, config, key, instance_name, target_version
    ):
        if not target_version:
            return False, f"No updates available for {process_name}."

        versions = Versions()
        install_dir = config.get("install_dir")
        if install_dir and key in (
            "sonarr",
            "radarr",
            "lidarr",
            "prowlarr",
            "readarr",
            "whisparr",
            "whisparr-v3",
        ):
            current_version, error = versions.read_arr_version_from_dir(
                key, install_dir
            )
        else:
            current_version, error = versions.version_check(
                process_name, instance_name, key
            )
        self.logger.info(
            f"{process_name} pinned version: {target_version} (current: {current_version or 'unknown'})."
        )
        if current_version == target_version:
            return False, f"No updates available for {process_name}."
        if not current_version:
            self.logger.warning(
                f"Failed to read current version for {process_name}: {error}"
            )

        self.logger.info(
            f"Updating {process_name} from {current_version or 'unknown'} to {target_version}."
        )
        if process_name in self.process_handler.process_names:
            self.stop_process(process_name)
        with self.process_handler.setup_tracker_lock:
            if process_name in self.process_handler.setup_tracker:
                self.process_handler.setup_tracker.remove(process_name)

        success, error = setup_project(self.process_handler, process_name)
        if not success:
            return (
                False,
                f"Failed to update {process_name} to {target_version}: {error}",
            )

        self.start_process(process_name, config, key, instance_name)
        return True, f"Updated {process_name} to {target_version}."

    def update_check_jellyfin_latest(self, process_name, config, key, instance_name):
        jellyfin_service_path = "/usr/lib/jellyfin/bin/jellyfin"
        if not os.path.exists(jellyfin_service_path):
            self.logger.info(
                f"{process_name} not installed yet; deferring install to setup."
            )
            return False, f"No updates available for {process_name}."

        versions = Versions()
        current_version, error = versions.version_check(
            process_name, instance_name, key
        )
        latest_version, latest_error = self.get_jellyfin_latest_version()
        if not latest_version:
            return False, f"Failed to get latest Jellyfin version: {latest_error}"
        self.logger.info(
            f"Jellyfin latest version: {latest_version} (current: {current_version or 'unknown'})."
        )
        if current_version == latest_version:
            return False, f"No updates available for {process_name}."

        self.logger.info(
            f"Updating {process_name} from {current_version or 'unknown'} to {latest_version}."
        )
        if process_name in self.process_handler.process_names:
            self.stop_process(process_name)
        with self.process_handler.setup_tracker_lock:
            if process_name in self.process_handler.setup_tracker:
                self.process_handler.setup_tracker.remove(process_name)

        installer = JellyfinInstaller()
        success, error = installer.install_jellyfin_server()
        if not success:
            return (
                False,
                f"Failed to update {process_name} to {latest_version}: {error}",
            )

        success, error = setup_project(self.process_handler, process_name)
        if not success:
            return (
                False,
                f"Failed to update {process_name} to {latest_version}: {error}",
            )

        self.start_process(process_name, config, key, instance_name)
        return True, f"Updated {process_name} to {latest_version}."

    def update_check_emby_latest(self, process_name, config, key, instance_name):
        versions = Versions()
        current_version, error = versions.version_check(
            process_name, instance_name, key
        )
        latest_version, latest_error = self.get_emby_latest_version(config)
        if not latest_version:
            return False, f"Failed to get latest Emby version: {latest_error}"
        self.logger.info(
            f"Emby latest version: {latest_version} (current: {current_version or 'unknown'})."
        )
        if current_version == latest_version:
            return False, f"No updates available for {process_name}."

        self.logger.info(
            f"Updating {process_name} from {current_version or 'unknown'} to {latest_version}."
        )
        if process_name in self.process_handler.process_names:
            self.stop_process(process_name)
        with self.process_handler.setup_tracker_lock:
            if process_name in self.process_handler.setup_tracker:
                self.process_handler.setup_tracker.remove(process_name)

        original_release_enabled = config.get("release_version_enabled")
        original_release_version = config.get("release_version")
        config["release_version_enabled"] = True
        config["release_version"] = latest_version
        try:
            success, error = setup_project(self.process_handler, process_name)
            if not success:
                return (
                    False,
                    f"Failed to update {process_name} to {latest_version}: {error}",
                )
        finally:
            config["release_version_enabled"] = original_release_enabled
            config["release_version"] = original_release_version

        self.start_process(process_name, config, key, instance_name)
        return True, f"Updated {process_name} to {latest_version}."

    def update_check_arr_latest(self, process_name, config, key, instance_name):
        versions = Versions()
        install_dir = config.get("install_dir")
        if install_dir:
            current_version, error = versions.read_arr_version_from_dir(
                key, install_dir
            )
        else:
            current_version, error = versions.version_check(
                process_name, instance_name, key
            )
        installer = ArrInstaller(key, install_dir=install_dir)
        latest_version, latest_error = installer.get_latest_version()
        if not latest_version:
            return False, f"Failed to get latest {key} version: {latest_error}"
        self.logger.info(
            f"{key.capitalize()} latest version: {latest_version} (current: {current_version or 'unknown'})."
        )
        if current_version == latest_version:
            return False, f"No updates available for {process_name}."

        self.logger.info(
            f"Updating {process_name} from {current_version or 'unknown'} to {latest_version}."
        )
        if process_name in self.process_handler.process_names:
            self.stop_process(process_name)
        with self.process_handler.setup_tracker_lock:
            if process_name in self.process_handler.setup_tracker:
                self.process_handler.setup_tracker.remove(process_name)

        success, error = installer.install()
        if not success:
            return (
                False,
                f"Failed to update {process_name} to {latest_version}: {error}",
            )

        success, error = setup_project(self.process_handler, process_name)
        if not success:
            return (
                False,
                f"Failed to update {process_name} to {latest_version}: {error}",
            )

        self.start_process(process_name, config, key, instance_name)
        return True, f"Updated {process_name} to {latest_version}."

    def get_jellyfin_latest_version(self):
        try:
            result = subprocess.run(
                ["apt-cache", "policy", "jellyfin"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("Candidate:"):
                        candidate = line.split(":", 1)[1].strip()
                        if candidate and candidate != "(none)":
                            return candidate, None
        except Exception as e:
            return None, str(e)
        return None, "Candidate version not found"

    def get_emby_latest_version(self, config):
        try:
            repo_owner = config.get("repo_owner")
            repo_name = config.get("repo_name")
            if not repo_owner or not repo_name:
                return None, "Emby repo owner/name not configured"
            downloader = Versions().downloader
            latest_release_version, error = downloader.get_latest_release(
                repo_owner, repo_name, nightly=False
            )
            if not latest_release_version:
                return None, error
            return latest_release_version, None
        except Exception as e:
            return None, str(e)

    def update_check_plex(self, process_name, config, key, instance_name):
        installer = PlexInstaller()
        plex_media_server_dir = config.get(
            "plex_media_server_dir", "/usr/lib/plexmediaserver"
        )
        if not os.path.exists(plex_media_server_dir):
            pinned_version = installer.normalize_version(config.get("pinned_version"))
            install_label = pinned_version or "latest"
            self.logger.info(
                f"Plex Media Server not found; installing {install_label} for {process_name}."
            )
            success, error = installer.install_plex_media_server(version=pinned_version)
            if not success:
                return (
                    False,
                    f"Failed to install {process_name} ({install_label}): {error}",
                )

            success, error = setup_project(self.process_handler, process_name)
            if not success:
                return (
                    False,
                    f"Failed to install {process_name} ({install_label}): {error}",
                )

            self.start_process(process_name, config, key, instance_name)
            return True, f"Installed {process_name} ({install_label})."

        update_needed, update_info = installer.check_for_update(
            process_name, instance_name
        )
        if not update_needed:
            return False, f"{update_info} for {process_name}."

        pinned_version = config.get("pinned_version")
        if pinned_version:
            if update_info.get("current_version") == pinned_version:
                return False, f"Plex pinned to {pinned_version} for {process_name}."
            if update_info.get("latest_version") != pinned_version:
                return (
                    False,
                    f"Plex pinned to {pinned_version}; latest is {update_info.get('latest_version')} for {process_name}.",
                )

        self.logger.info(
            f"Updating {process_name} from {update_info.get('current_version')} to {update_info.get('latest_version')}."
        )
        if process_name in self.process_handler.process_names:
            self.stop_process(process_name)
        with self.process_handler.setup_tracker_lock:
            if process_name in self.process_handler.setup_tracker:
                self.process_handler.setup_tracker.remove(process_name)

        success, error = installer.install_plex_media_server()
        if not success:
            return (
                False,
                f"Failed to update {process_name} to {update_info.get('latest_version')}: {error}",
            )

        success, error = setup_project(self.process_handler, process_name)
        if not success:
            return (
                False,
                f"Failed to update {process_name} to {update_info.get('latest_version')}: {error}",
            )

        self.start_process(process_name, config, key, instance_name)
        return True, f"Updated {process_name} to {update_info.get('latest_version')}."

    def stop_process(self, process_name):
        self.process_handler.stop_process(process_name)

    def start_process(self, process_name, config, key, instance_name):
        refreshed_key, refreshed_instance = CONFIG_MANAGER.find_key_for_process(
            process_name
        )
        if refreshed_key:
            config = (
                CONFIG_MANAGER.get_instance(refreshed_instance, refreshed_key) or config
            )
            key = refreshed_key
            instance_name = refreshed_instance

        if config.get("wait_for_dir", False):
            sleep_s = 10
            while not os.path.exists(wait_dir := config["wait_for_dir"]):
                if self.process_handler.shutting_down:
                    self.logger.info(
                        "Shutdown requested; skipping wait for directory %s.",
                        wait_dir,
                    )
                    return False, "Shutdown requested"
                self.logger.info(
                    f"Waiting for directory {wait_dir} to become available before starting {process_name}"
                )
                time.sleep(sleep_s)
                sleep_s = min(60, int(sleep_s * 1.5))

        wait_mounts = config.get("wait_for_mounts") or []
        if wait_mounts:
            sleep_s = 10
            while True:
                if self.process_handler.shutting_down:
                    self.logger.info(
                        "Shutdown requested; skipping wait for mounts before %s.",
                        process_name,
                    )
                    return False, "Shutdown requested"
                missing = [
                    mount_path
                    for mount_path in wait_mounts
                    if not os.path.ismount(mount_path)
                ]
                if not missing:
                    break
                self.logger.info(
                    "Waiting for mounts to become available before starting %s: %s",
                    process_name,
                    ", ".join(missing),
                )
                time.sleep(sleep_s)
                sleep_s = min(60, int(sleep_s * 1.5))

        if config.get("wait_for_url", False):
            wait_for_urls = config["wait_for_url"]
            time.sleep(5)
            start_time = time.time()

            for wait_entry in wait_for_urls:
                wait_url = wait_entry["url"]
                auth = wait_entry.get("auth", None)

                logger.info(
                    f"Waiting to start {process_name} until {wait_url} is accessible."
                )

                sleep_s = 5
                while time.time() - start_time < 600:
                    if self.process_handler.shutting_down:
                        self.logger.info(
                            "Shutdown requested; skipping wait for %s.",
                            wait_url,
                        )
                        return False, "Shutdown requested"
                    try:
                        if auth:
                            response = requests.get(
                                wait_url, auth=(auth["user"], auth["password"])
                            )
                            # logger.debug(
                            #    f"Authenticating to {wait_url} with {auth['user']}:{auth['password']}"
                            # )
                        else:
                            response = requests.get(wait_url)

                        if 200 <= response.status_code < 300:
                            logger.info(
                                f"{wait_url} is accessible with {response.status_code}."
                            )
                            break
                        else:
                            logger.debug(
                                f"Received status code {response.status_code} while waiting for {wait_url} to be accessible."
                            )
                    except requests.RequestException as e:
                        logger.debug(f"Waiting for {wait_url}: {e}")
                    time.sleep(sleep_s)
                    sleep_s = min(60, int(sleep_s * 1.5))
                else:
                    raise RuntimeError(
                        f"Timeout: {wait_url} is not accessible after 600 seconds."
                    )

        command = config["command"]
        config_dir = config["config_dir"]

        if config.get("suppress_logging", False):
            self.logger.info(f"Suppressing {process_name} logging")
            suppress_logging = True
        else:
            suppress_logging = False

        if key == "riven_backend":
            if not os.path.exists(os.path.join(config_dir, "data", "settings.json")):
                from utils.riven_settings import set_env_variables

                logger.info("Riven initial setup for first run")
                threading.Thread(target=set_env_variables).start()

        env = os.environ.copy()
        env.update(config.get("env", {}))

        process, error = self.process_handler.start_process(
            process_name,
            config_dir,
            command,
            instance_name,
            suppress_logging=suppress_logging,
            env=env,
        )
        if self.process_handler.shutting_down:
            return process, "Shutdown requested"
        if key == "riven_backend":
            from utils.riven_settings import load_settings

            time.sleep(10)
            load_settings()

        if key == "decypharr":
            if self.process_handler.shutting_down:
                return process, "Shutdown requested"
            from utils.decypharr_settings import patch_decypharr_config

            time.sleep(10)
            patched, error = patch_decypharr_config()
            if patched:
                self.logger.info("Restarting Decypharr to apply new config")
                self.process_handler.stop_process(process_name)
                self.process_handler.start_process(
                    process_name,
                    config_dir,
                    command,
                    instance_name,
                    suppress_logging=suppress_logging,
                    env=env,
                )
            elif error:
                self.logger.warning("Decypharr config patch failed: %s", error)

        if key == "nzbdav":
            if self.process_handler.shutting_down:
                return process, "Shutdown requested"
            from utils.nzbdav_settings import patch_nzbdav_config

            time.sleep(10)
            patched, error = patch_nzbdav_config()
            if patched:
                self.logger.info("Restarting NzbDAV to apply new config")
                self.process_handler.stop_process(process_name)
                self.process_handler.start_process(
                    process_name,
                    config_dir,
                    command,
                    instance_name,
                    suppress_logging=suppress_logging,
                    env=env,
                )
            elif error:
                self.logger.warning("NzbDAV config patch failed: %s", error)

        if key in [
            "prowlarr",
            "sonarr",
            "radarr",
            "lidarr",
            "readarr",
            "whisparr",
            "whisparr-v3",
        ]:
            if self.process_handler.shutting_down:
                return process, "Shutdown requested"
            prowlarr_cfg = CONFIG_MANAGER.get("prowlarr") or {}
            if isinstance(prowlarr_cfg.get("instances"), dict):
                prowlarr_enabled = any(
                    isinstance(inst, dict) and inst.get("enabled")
                    for inst in prowlarr_cfg["instances"].values()
                )
            else:
                prowlarr_enabled = bool(prowlarr_cfg.get("enabled"))
            if not prowlarr_enabled:
                return process, None

            from utils.prowlarr_settings import patch_prowlarr_apps

            time.sleep(10)
            ok, err = patch_prowlarr_apps()
            if not ok and err:
                self.logger.warning("Prowlarr app sync failed: %s", err)

        if key == "plex":
            if self.process_handler.shutting_down:
                return process, "Shutdown requested"
            from utils.plex_settings import patch_plex_config

            time.sleep(10)
            patched, error = patch_plex_config()
            if patched:
                self.logger.info("Restarting Plex to apply new config")
                self.process_handler.stop_process(process_name)
                self.process_handler.start_process(
                    process_name,
                    config_dir,
                    command,
                    instance_name,
                    suppress_logging=suppress_logging,
                    env=env,
                )

        return process, error
