from utils.global_logger import logger
import subprocess, os


class JellyfinInstaller:
    def __init__(self):
        self.logger = logger

    def download_and_install_jellyfin_gpg_key(self):
        curl = subprocess.Popen(
            ["curl", "-fsSL", "https://repo.jellyfin.org/jellyfin_team.gpg.key"],
            stdout=subprocess.PIPE,
        )
        gpg = subprocess.Popen(
            ["gpg", "--dearmor", "--yes", "-o", "/etc/apt/keyrings/jellyfin.gpg"],
            stdin=curl.stdout,
        )
        curl.stdout.close()
        gpg.communicate()
        if curl.wait() != 0 or gpg.returncode != 0:
            raise subprocess.CalledProcessError(
                returncode=1, cmd="curl | gpg --dearmor"
            )

    def install_jellyfin_server(self, version=None):
        try:
            if version:
                self.logger.info(
                    f"Installing Jellyfin media server version {version}..."
                )
            else:
                self.logger.info("Installing Jellyfin media server...")

            # Step 1: Ensure required tools
            subprocess.run(["apt", "update"], check=True)
            subprocess.run(["apt", "install", "-y", "gnupg", "curl"], check=True)

            # Step 2: Add universe repo
            with open("/etc/os-release") as f:
                os_release = f.read().lower()
            if "ubuntu" in os_release:
                subprocess.run(["add-apt-repository", "-y", "universe"], check=True)

            # Step 3: Create keyring and download GPG key
            os.makedirs("/etc/apt/keyrings", exist_ok=True)
            self.download_and_install_jellyfin_gpg_key()

            # Step 4: Create sources file
            version_os = subprocess.check_output(
                ["awk", "-F=", "/^ID=/{print $2}"],
                text=True,
                input=open("/etc/os-release").read(),
            ).strip()
            version_codename = subprocess.check_output(
                ["awk", "-F=", "/^VERSION_CODENAME=/{print $2}"],
                text=True,
                input=open("/etc/os-release").read(),
            ).strip()
            dpkg_arch = subprocess.check_output(
                ["dpkg", "--print-architecture"], text=True
            ).strip()

            sources_content = (
                f"Types: deb\n"
                f"URIs: https://repo.jellyfin.org/{version_os}\n"
                f"Suites: {version_codename}\n"
                f"Components: main\n"
                f"Architectures: {dpkg_arch}\n"
                f"Signed-By: /etc/apt/keyrings/jellyfin.gpg\n"
            )
            with open("/etc/apt/sources.list.d/jellyfin.sources", "w") as f:
                f.write(sources_content)

            # Step 5: Update apt
            subprocess.run(["apt", "update"], check=True)

            # Step 6: Install jellyfin metapackage
            if version:
                subprocess.run(
                    ["apt", "install", "-y", f"jellyfin={version}"], check=True
                )
            else:
                subprocess.run(["apt", "install", "-y", "jellyfin"], check=True)

            # Step 7: Ensure web client is available
            expected_web_path = "/usr/lib/jellyfin/bin/jellyfin-web"
            real_web_path = "/usr/share/jellyfin/web"
            if not os.path.exists(expected_web_path):
                if os.path.exists(real_web_path):
                    os.symlink(real_web_path, expected_web_path)
                    self.logger.info(
                        f"Symlinked web client: {expected_web_path} -> {real_web_path}"
                    )
                else:
                    self.logger.warning(
                        f"Expected web client source not found at {real_web_path}"
                    )

            self.logger.info("Jellyfin media server installed successfully.")
            return True, None

        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to install Jellyfin: {e}")
            return False, str(e)
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            return False, str(e)
