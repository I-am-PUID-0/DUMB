from utils.global_logger import logger
from utils.download import Downloader
from utils.config_loader import CONFIG_MANAGER
import subprocess, json, re, requests


class Versions:
    def __init__(self):
        self.logger = logger
        self.downloader = Downloader()

    def version_check(
        self, process_name=None, instance_name=None, key=None, version_path=None
    ):
        try:
            if key == "dumb_api_service":
                version_path = "/pyproject.toml"
                is_file = True
            elif key == "dumb_frontend":
                version_path = "/dumb/frontend/package.json"
                is_file = True
            elif key == "decypharr":
                version_path = "/decypharr/version.txt"
                is_file = True
            elif key == "riven_frontend":
                version_path = "/riven/frontend/version.txt"
                is_file = True
            elif key == "cli_debrid":
                version_path = "/cli_debrid/version.txt"
                is_file = True
            elif key == "cli_battery":
                version_path = "/cli_debrid/cli_battery/version.txt"
                is_file = True
            elif key == "phalanx_db":
                version_path = "/phalanx_db/version.txt"
                is_file = True
            elif key == "riven_backend":
                version_path = "/riven/backend/pyproject.toml"
                is_file = True
            elif key == "zilean":
                version_path = "/zilean/version.txt"
                is_file = True
            elif key == "zurg":
                config = CONFIG_MANAGER.get_instance(instance_name, key)
                if not config:
                    raise ValueError(f"Configuration for {process_name} not found.")
                version_path = config.get("config_dir") + "/zurg"
                is_file = False
            elif key == "jellyfin":
                try:
                    config = CONFIG_MANAGER.get_instance(instance_name, key)
                    if not config:
                        raise ValueError(f"Configuration for {process_name} not found.")
                    result = subprocess.run(
                        ["dpkg-query", "-W", "-f=${Version}", "jellyfin"],
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        return result.stdout.strip(), None
                    port = config.get("port", 8096)
                    if not port:
                        raise ValueError("Jellyfin port not configured.")
                    url = f"http://localhost:{port}/System/Info/Public"
                    response = requests.get(url, timeout=3)
                    version = response.json().get("Version", None)
                    if version:
                        return version, None
                    else:
                        return None, "Jellyfin version not found in response"
                except Exception as e:
                    return None, f"Error fetching Jellyfin version: {e}"
            elif key == "emby":
                config = CONFIG_MANAGER.get_instance(instance_name, key)
                if not config:
                    raise ValueError(f"Configuration for {process_name} not found.")
                version_path = config.get("version_path", "/emby/version.txt")
                is_file = True
            ### update this once bazarr is implemented
            elif key == "bazarr":
                return None, "Bazarr version check not implemented"

            elif key in (
                "sonarr",
                "radarr",
                "prowlarr",
                "lidarr",
                "readarr",
                "whisparr",
                "whisparr-v3",
            ):
                try:
                    dll_path = (
                        f"/opt/{key}/{key.capitalize()}/{key.capitalize()}.Core.dll"
                    )
                    grep_string = f"{key.capitalize()}.Common, Version="
                    result = subprocess.run(
                        ["strings", dll_path], capture_output=True, text=True
                    )
                    if result.returncode != 0:
                        return None, f"Failed to run strings on {dll_path}"
                    matches = [
                        line
                        for line in result.stdout.splitlines()
                        if grep_string in line
                    ]
                    if matches:
                        match = re.search(r"Version=([\d\.]+)", matches[0])
                        if match:
                            return match.group(1), None
                    return None, f"{key.capitalize()} version not found in Core.dll"
                except FileNotFoundError:
                    return None, f"{key.capitalize()}.Core.dll not found"
                except Exception as e:
                    return None, f"Error reading {key} version: {e}"
            elif key == "plex":
                try:
                    result = subprocess.run(
                        ["/usr/lib/plexmediaserver/Plex Media Server", "--version"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=5,
                    )
                    if result.returncode == 0:
                        version = result.stdout.strip()
                        return version, None
                    return None, f"Failed to get version: {result.stderr.strip()}"
                except Exception as e:
                    return None, f"Error running Plex binary: {e}"
            elif key == "postgres":
                try:
                    result = subprocess.run(
                        ["psql", "--version"], capture_output=True, text=True
                    )
                    if result.returncode == 0:
                        version = result.stdout.strip().split()[-1]
                        return version, None
                    return None, "psql not found or failed"
                except FileNotFoundError:
                    return None, "psql binary not found"
            elif key == "pgadmin":
                try:
                    import glob

                    version_files = glob.glob(
                        "/pgadmin/venv/lib/python*/site-packages/pgadmin4/version.py"
                    )
                    if version_files:
                        version_globals = {}
                        with open(version_files[0], "r") as f:
                            code = f.read()
                            exec(code, version_globals)
                        release = version_globals.get("APP_RELEASE")
                        revision = version_globals.get("APP_REVISION")
                        suffix = version_globals.get("APP_SUFFIX", "")
                        if release is not None and revision is not None:
                            version = f"{release}.{revision}"
                            if suffix:
                                version += f"-{suffix}"
                            return version, None
                    return None, "pgAdmin version info not found"
                except Exception as e:
                    return None, f"Error extracting pgAdmin version: {e}"
            elif key == "rclone":
                try:
                    result = subprocess.run(
                        ["rclone", "--version"], capture_output=True, text=True
                    )
                    if result.returncode == 0:
                        version_line = result.stdout.strip().splitlines()[0]
                        version = version_line.split()[1]
                        return version, None
                    else:
                        return None, "rclone --version failed"
                except FileNotFoundError:
                    return None, "rclone binary not found"
                except Exception as e:
                    return None, f"Error reading rclone version: {e}"
            elif key == "plex_debrid":
                version_path = "/plex_debrid/ui/ui_settings.py"
                is_file = True

            if is_file:
                try:
                    with open(version_path, "r") as f:
                        if key == "dumb_frontend":
                            try:
                                data = json.load(f)
                                version = f'v{data["version"]}'
                            except (json.JSONDecodeError, KeyError) as e:
                                version = None
                        elif (
                            key == "riven_frontend"
                            or key == "cli_debrid"
                            or key == "cli_battery"
                            or key == "phalanx_db"
                        ):
                            version = f"v{f.read().strip()}"
                        elif (
                            key == "riven_backend"
                            or key == "dumb_api_service"
                            or key == "plex_debrid"
                        ):
                            for line in f:
                                if line.startswith("version = "):
                                    version_raw = (
                                        line.split("=")[1].strip().strip('"').strip("'")
                                    )
                                    match = re.search(r"v?\d+(\.\d+)*", version_raw)
                                    version = match.group(0) if match else ""
                                    if key == "riven_backend":
                                        version = f"v{version}"
                                    break
                            else:
                                version = None
                        elif key == "zilean" or key == "decypharr" or key == "emby":
                            version = f.read().strip()
                        if version:
                            return version, None
                        else:
                            return None, "Version not found"
                except FileNotFoundError:
                    return None, f"Version file not found: {version_path}"
            if not is_file:
                try:
                    result = subprocess.run(
                        [version_path, "version"], capture_output=True, text=True
                    )
                    if result.returncode == 0:
                        version_info = result.stdout.strip()
                        version = version_info.split("\n")[-1].split(": ")[-1]
                        return version, None
                    else:
                        return None, "Version not found"
                except FileNotFoundError:
                    return None, f"Version file not found: {version_path}"
        except Exception as e:
            self.logger.error(
                f"Error reading current version for {process_name} from {version_path}: {e}"
            )
            return None, str(e)

    def version_write(self, process_name, key=None, version_path=None, version=None):
        try:
            if key == "zilean":
                version_path = "/zilean/version.txt"
                with open(version_path, "w") as f:
                    f.write(version)
            elif key == "decypharr":
                version_path = "/decypharr/version.txt"
                with open(version_path, "w") as f:
                    f.write(version)
            elif key == "emby":
                version_path = version_path or "/emby/version.txt"
                with open(version_path, "w") as f:
                    f.write(version)
            return True, None
        except FileNotFoundError:
            return False, f"Version file not found: {version_path}"
        except Exception as e:
            return False, str(e)

    def compare_versions(
        self,
        process_name,
        repo_owner,
        repo_name,
        instance_name,
        key,
        nightly=False,
        prerelease=False,
    ):
        try:
            latest_release_version, error = self.downloader.get_latest_release(
                repo_owner, repo_name, nightly=nightly, prerelease=prerelease
            )
            if not latest_release_version:
                self.logger.error(
                    f"Failed to get the latest release for {process_name}: {error}"
                )
                raise Exception(error)
            current_version, error = self.version_check(
                process_name, instance_name, key
            )
            if not current_version:
                self.logger.error(
                    f"Failed to get the current version for {process_name}: {error}"
                )
                current_version = "0.0.0"
                self.logger.error(
                    f"Setting current version to 0.0.0 for {process_name}"
                )
                # raise Exception(error)
            if nightly:
                current_date = ".".join(current_version.split(".")[0:3])
                latest_date = ".".join(latest_release_version.split(".")[0:3])
                if current_date == latest_date:
                    return False, {
                        "message": "No updates available (same nightly date)",
                        "current_version": current_version,
                    }
            if current_version == latest_release_version:
                return False, {
                    "message": "No updates available",
                    "current_version": current_version,
                }
            else:
                return True, {
                    "message": "Update available",
                    "current_version": current_version,
                    "latest_version": latest_release_version,
                }
        except Exception as e:
            self.logger.error(
                f"Exception during version comparison {process_name}: {e}"
            )
            return False, str(e)
