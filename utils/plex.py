from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
import os, platform, subprocess, tempfile, requests
import xml.etree.ElementTree as ET


class PlexInstaller:
    def __init__(self):
        self.logger = logger

    def get_architecture(self):
        system_arch = platform.machine().lower()
        system_os = platform.system().lower()

        if system_arch in ("x86_64", "amd64") and system_os == "linux":
            return "linux-x86_64"
        elif system_arch in ("aarch64", "arm64") and system_os == "linux":
            return "linux-aarch64"
        else:
            self.logger.error(f"Unsupported architecture: {system_arch} / {system_os}")
            return None

    def get_download_info(self, build, distro="debian", channel=16, token=None):
        url = f"https://plex.tv/downloads/details/5?build={build}&channel={channel}&distro={distro}"
        if CONFIG_MANAGER.get("dumb").get("plex_token"):
            token = CONFIG_MANAGER.get("dumb").get("plex_token")
            url += f"&X-Plex-Token={token}"

        self.logger.info(f"Fetching Plex version info from: {url}")
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if response.status_code != 200:
            raise Exception(
                f"Failed to fetch version info. Status: {response.status_code}"
            )

        try:
            root = ET.fromstring(response.content)
            release = root.find(".//Release")
            if release is None:
                raise Exception("No <Release> found in version info")

            version = release.attrib.get("version")
            package = release.find("Package")
            if package is None:
                raise Exception("No <Package> element found in <Release>")

            file_url = package.attrib.get("url")
            if not file_url:
                raise Exception("No download URL found in <Package>")

            self.logger.debug(f"Found version: {version}, URL: {file_url}")
            return version, file_url

        except ET.ParseError as e:
            raise Exception(f"Failed to parse XML: {e}")

    def install_plex_media_server(self):
        build = self.get_architecture()
        if not build:
            return False, "Unsupported architecture"

        try:
            version, download_url = self.get_download_info(build)
            self.logger.info(
                f"Installing Plex Media Server v{version} from: {download_url}"
            )

            with tempfile.NamedTemporaryFile(delete=False, suffix=".deb") as tmp_file:
                self.logger.info(f"Downloading to temporary file: {tmp_file.name}")
                with requests.get(download_url, stream=True) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(chunk_size=8192):
                        tmp_file.write(chunk)

            subprocess.run(
                [
                    "dpkg",
                    "-i",
                    "--force-confold",
                    "--force-architecture",
                    tmp_file.name,
                ],
                check=True,
            )
            self.logger.info("Plex Media Server installed successfully.")
            os.remove(tmp_file.name)
            return True, None

        except Exception as e:
            self.logger.error(f"Error installing Plex Media Server: {e}")
            return False, str(e)


def perform_plex_claim(claim_token, preferences_path, logger):
    import os, uuid, hashlib, xml.etree.ElementTree as ET, requests

    if not os.path.exists(preferences_path):
        os.makedirs(os.path.dirname(preferences_path), exist_ok=True)
        with open(preferences_path, "w") as f:
            f.write('<?xml version="1.0" encoding="utf-8"?><Preferences/>')

    tree = ET.parse(preferences_path)
    root = tree.getroot()

    def get_attr(attr):
        return root.attrib.get(attr)

    def set_attr(attr, value):
        root.set(attr, value)

    if get_attr("PlexOnlineToken"):
        logger.info("Plex server already claimed.")
        return True, None

    machine_id = get_attr("MachineIdentifier") or str(uuid.uuid4())
    set_attr("MachineIdentifier", machine_id)

    processed_id = (
        get_attr("ProcessedMachineIdentifier")
        or hashlib.sha1(f"{machine_id}- Plex Media Server".encode()).hexdigest()
    )
    set_attr("ProcessedMachineIdentifier", processed_id)

    headers = {
        "X-Plex-Client-Identifier": processed_id,
        "X-Plex-Product": "Plex Media Server",
        "X-Plex-Version": "1.1",
        "X-Plex-Provides": "server",
        "X-Plex-Platform": "Linux",
        "X-Plex-Platform-Version": "1.0",
        "X-Plex-Device-Name": "PlexMediaServer",
        "X-Plex-Device": "Linux",
    }

    try:
        logger.info("Attempting to claim Plex server using token...")
        response = requests.post(
            f"https://plex.tv/api/claim/exchange?token={claim_token}",
            headers=headers,
            timeout=10,
        )
        if response.status_code != 200:
            logger.error(f"Claim failed: HTTP {response.status_code} - {response.text}")
            return False, f"Claim failed: HTTP {response.status_code}"

        auth_token = ET.fromstring(response.text).findtext("authentication-token")
        if auth_token:
            set_attr("PlexOnlineToken", auth_token)
            tree.write(preferences_path)
            logger.info("Plex server claimed successfully.")
            return True, None
        else:
            logger.warning("No authentication token found in response.")
            return False, "No authentication token found in response."
    except Exception as e:
        logger.error(f"Claim request failed: {e}")
        return False, str(e)
