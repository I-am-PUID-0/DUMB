from utils.global_logger import logger
import platform, subprocess, os, re, requests, json


class ArrInstaller:
    def __init__(
        self,
        app_name: str,
        version: str = "4",
        branch: str = "main",
        install_dir=None,
    ):
        self.logger = logger
        self.app_name = app_name.lower()
        self.app_name_cap = app_name.capitalize()
        self.version = version
        self.branch = branch
        self.install_dir = install_dir or f"/opt/{self.app_name}"

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
        version_query = ""
        if self.version and self.version not in ("3", "4"):
            version_query = f"&version={self.version}"
        base_url = f"https://{self.app_name}.servarr.com/v1/update/{self.branch}/updatefile?os=linux&runtime=netcore{version_query}"
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

    def resolve_pinned_download_url(self):
        if not self.version or self.version in ("3", "4"):
            return None, "Pinned version not set for update feed lookup."
        arch = platform.machine()
        if arch == "x86_64":
            arch_query = "x64"
        elif arch == "aarch64":
            arch_query = "arm64"
        elif arch == "armv7l":
            arch_query = "arm"
        else:
            return None, f"Unsupported architecture: {arch}"

        base_url = (
            f"https://{self.app_name}.servarr.com/v1/update/{self.branch}/changes"
            f"?os=linux&runtime=netcore&arch={arch_query}"
        )
        if self.app_name == "sonarr":
            base_url = (
                f"https://services.{self.app_name}.tv/v1/update/{self.branch}/changes"
                f"?os=linux&arch={arch_query}"
            )
        try:
            response = requests.get(base_url, timeout=10)
            if response.status_code != 200:
                return None, f"Failed to fetch update feed: {response.status_code}"
            data = response.json()
        except Exception as e:
            return None, f"Failed to fetch update feed: {e}"

        candidates = []
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            for key in ("updates", "changes", "releases"):
                if isinstance(data.get(key), list):
                    candidates = data.get(key)
                    break

        for item in candidates:
            item_version = item.get("version") or item.get("Version")
            if item_version and (
                item_version == self.version
                or item_version.startswith(f"{self.version}.")
            ):
                download_url = (
                    item.get("url")
                    or item.get("updateFile")
                    or item.get("downloadUrl")
                    or item.get("download_url")
                )
                if download_url:
                    return download_url, None
        return None, f"Version {self.version} not found in update feed."

    def resolve_pinned_github_download_url(self):
        repo_map = {
            "radarr": ("Radarr", "Radarr"),
            "sonarr": ("Sonarr", "Sonarr"),
            "lidarr": ("Lidarr", "Lidarr"),
            "prowlarr": ("Prowlarr", "Prowlarr"),
            "readarr": ("Readarr", "Readarr"),
            "whisparr": ("Whisparr", "Whisparr"),
            "whisparr-v3": ("Whisparr", "Whisparr"),
        }
        if self.app_name not in repo_map:
            return None, f"GitHub repo mapping not found for {self.app_name}."
        owner, repo = repo_map[self.app_name]

        headers = {"Accept": "application/vnd.github.v3+json"}
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_API_TOKEN")
        if token:
            headers["Authorization"] = f"token {token}"

        def fetch_release_by_tag(tag):
            url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                return response.json(), None
            return None, f"Tag lookup failed ({response.status_code})"

        version_tag = f"v{self.version}"
        release, error = fetch_release_by_tag(version_tag)
        if not release:
            release, error = fetch_release_by_tag(self.version)
        if not release:
            url = f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=50"
            try:
                response = requests.get(url, headers=headers, timeout=10)
                if response.status_code != 200:
                    return (
                        None,
                        f"Failed to fetch releases list: {response.status_code}",
                    )
                releases = response.json()
                for item in releases:
                    tag = item.get("tag_name", "")
                    if self.version in tag:
                        release = item
                        break
            except Exception as e:
                return None, f"Failed to fetch releases list: {e}"
        if not release:
            return None, f"Version {self.version} not found on GitHub releases."

        assets = release.get("assets", [])
        if not assets:
            return None, f"No assets found for GitHub release {release.get('tag_name')}"

        arch = platform.machine()
        if arch == "x86_64":
            arch_key = "x64"
        elif arch == "aarch64":
            arch_key = "arm64"
        elif arch == "armv7l":
            arch_key = "arm"
        else:
            return None, f"Unsupported architecture: {arch}"

        for asset in assets:
            name = asset.get("name", "").lower()
            if (
                self.version in name
                and "linux" in name
                and arch_key in name
                and name.endswith(".tar.gz")
            ):
                return asset.get("browser_download_url"), None

        return None, f"No matching Linux {arch_key} tarball found on GitHub."

    def install(self):
        try:
            logger.info(f"Installing {self.app_name_cap}...")

            os.makedirs(self.install_dir, exist_ok=True)
            url = self.get_download_url()

            def download_with_wget(download_url):
                before_files = set(os.listdir(self.install_dir))
                subprocess.run(
                    ["wget", "--content-disposition", download_url],
                    check=True,
                    cwd=self.install_dir,
                )
                after_files = set(os.listdir(self.install_dir))
                return [name for name in (after_files - before_files) if name]

            new_files = download_with_wget(url)
            tarball = next((f for f in new_files if f.endswith(".tar.gz")), None)
            if not tarball and new_files:
                for name in new_files:
                    candidate_path = os.path.join(self.install_dir, name)
                    try:
                        with open(candidate_path, "rb") as f:
                            header = f.read(2)
                        if header == b"\x1f\x8b":
                            tarball = name
                            break
                    except Exception:
                        continue
            if not tarball:
                if new_files:
                    candidate_path = os.path.join(self.install_dir, new_files[0])
                    try:
                        with open(candidate_path, "r", encoding="utf-8") as f:
                            error_text = f.read().strip()
                        try:
                            error_json = json.loads(error_text)
                            download_url = (
                                error_json.get("url")
                                or error_json.get("updateFile")
                                or error_json.get("downloadUrl")
                                or error_json.get("download_url")
                            )
                            if download_url:
                                new_files = download_with_wget(download_url)
                                tarball = next(
                                    (f for f in new_files if f.endswith(".tar.gz")),
                                    None,
                                )
                                if not tarball and new_files:
                                    for name in new_files:
                                        candidate_path = os.path.join(
                                            self.install_dir, name
                                        )
                                        try:
                                            with open(candidate_path, "rb") as f:
                                                header = f.read(2)
                                            if header == b"\x1f\x8b":
                                                tarball = name
                                                break
                                        except Exception:
                                            continue
                                if tarball:
                                    pass
                                else:
                                    raise FileNotFoundError(
                                        f"Downloaded archive not found. Response: {error_text}"
                                    )
                            else:
                                pinned_url, pinned_error = (
                                    self.resolve_pinned_download_url()
                                )
                                if pinned_url:
                                    new_files = download_with_wget(pinned_url)
                                    tarball = next(
                                        (f for f in new_files if f.endswith(".tar.gz")),
                                        None,
                                    )
                                    if not tarball and new_files:
                                        for name in new_files:
                                            candidate_path = os.path.join(
                                                self.install_dir, name
                                            )
                                            try:
                                                with open(candidate_path, "rb") as f:
                                                    header = f.read(2)
                                                if header == b"\x1f\x8b":
                                                    tarball = name
                                                    break
                                            except Exception:
                                                continue
                                    if not tarball:
                                        raise FileNotFoundError(
                                            f"Downloaded archive not found. Response: {error_text}"
                                        )
                                else:
                                    github_url, github_error = (
                                        self.resolve_pinned_github_download_url()
                                    )
                                    if github_url:
                                        new_files = download_with_wget(github_url)
                                        tarball = next(
                                            (
                                                f
                                                for f in new_files
                                                if f.endswith(".tar.gz")
                                            ),
                                            None,
                                        )
                                        if not tarball and new_files:
                                            for name in new_files:
                                                candidate_path = os.path.join(
                                                    self.install_dir, name
                                                )
                                                try:
                                                    with open(
                                                        candidate_path, "rb"
                                                    ) as f:
                                                        header = f.read(2)
                                                    if header == b"\x1f\x8b":
                                                        tarball = name
                                                        break
                                                except Exception:
                                                    continue
                                        if not tarball:
                                            raise FileNotFoundError(
                                                f"Downloaded archive not found. {pinned_error} Response: {error_text}"
                                            )
                                    else:
                                        raise FileNotFoundError(
                                            f"Downloaded archive not found. {pinned_error} {github_error} Response: {error_text}"
                                        )
                        except json.JSONDecodeError:
                            raise FileNotFoundError(
                                f"Downloaded archive not found. Response: {error_text}"
                            )
                    except UnicodeDecodeError:
                        pass
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

    def extract_version_from_filename(self, filename):
        if not filename:
            return None
        match = re.search(r"(\d+(?:\.\d+)+)", filename)
        if match:
            return match.group(1)
        return None

    def get_latest_version(self):
        url = self.get_download_url()
        response = None
        try:
            response = requests.head(url, allow_redirects=True, timeout=10)
            if response.status_code >= 400:
                response = requests.get(
                    url, stream=True, allow_redirects=True, timeout=15
                )
            filename = None
            content_disposition = response.headers.get("Content-Disposition", "")
            match = re.search(r'filename="?([^"]+)"?', content_disposition)
            if match:
                filename = match.group(1)
            if not filename:
                filename = os.path.basename(response.url)
            version = self.extract_version_from_filename(filename)
            if version:
                return version, None
            return None, f"Unable to parse version from {filename}"
        except Exception as e:
            return None, str(e)
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
