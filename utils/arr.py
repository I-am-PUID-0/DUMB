from utils.global_logger import logger
import platform, subprocess, os


class ArrInstaller:
    def __init__(self, app_name: str, version: str = "4", branch: str = "main"):
        self.logger = logger
        self.app_name = app_name.lower()
        self.app_name_cap = app_name.capitalize()
        self.version = version
        self.branch = branch
        self.install_dir = f"/opt/{self.app_name}"

    def get_download_url(self):
        arch = platform.machine()
        ## set branch based on app_name: sonarr=main, radarr=master, prowlarr=master, readarr=develop, lidarr=master, whisparr=nightly, whisparr-v3=eros
        self.branch = (
            "main"
            if self.app_name == "sonarr"
            else (
                "master"
                if self.app_name in ["radarr", "prowlarr", "lidarr"]
                else (
                    "develop"
                    if self.app_name == "readarr"
                    else (
                        "nightly"
                        if self.app_name == "whisparr"
                        else "eros" if self.app_name == "whisparr-v3" else "main"
                    )
                )
            )
        )
        alt_base_url = f"https://services.{self.app_name}.tv/v1/download/{self.branch}/latest?version={self.version}&os=linux"
        base_url = f"https://{self.app_name}.servarr.com/v1/update/{self.branch}/updatefile?os=linux&runtime=netcore"
        if self.app_name == "sonarr":
            base_url = alt_base_url
        if arch == "x86_64":
            return f"{base_url}&arch=x64"
        elif arch == "aarch64":
            return f"{base_url}&arch=arm64"
        elif arch == "armv7l":
            return f"{base_url}&arch=arm"
        else:
            raise ValueError(f"Unsupported architecture: {arch}")

    def install(self):
        try:
            logger.info(f"Installing {self.app_name_cap}...")

            os.makedirs(self.install_dir, exist_ok=True)
            url = self.get_download_url()

            subprocess.run(
                ["wget", "--content-disposition", url],
                check=True,
                cwd=self.install_dir,
            )

            tarball = next(
                (f for f in os.listdir(self.install_dir) if f.endswith(".tar.gz")), None
            )
            if not tarball:
                raise FileNotFoundError("Downloaded archive not found.")

            subprocess.run(["tar", "xzf", tarball], check=True, cwd=self.install_dir)
            os.remove(os.path.join(self.install_dir, tarball))
            binary_path = os.path.join(
                self.install_dir, self.app_name_cap, self.app_name_cap
            )
            if os.path.exists(binary_path):
                os.chmod(binary_path, 0o755)
            logger.info(f"{self.app_name_cap} installed successfully.")
            return True, None

        except Exception as e:
            logger.error(f"Failed to install {self.app_name_cap}: {e}")
            return False, str(e)
