from utils.global_logger import logger
from utils.config_loader import CONFIG_MANAGER
from utils.install_cache import INSTALL_CACHE
import requests, time, os, zipfile, io, shutil, platform, re, tarfile, tempfile, stat, hashlib, errno
import fnmatch
from pathlib import Path
from urllib.parse import quote


def _replace_cross_device_safe(source, destination):
    # os.replace() requires source and destination to be on the same
    # filesystem. Bind-mounted persistent targets (e.g. /infinidysk) are a
    # separate mount from the container's own root filesystem, so a plain
    # os.replace() raises EXDEV here even though the paths look local.
    try:
        os.replace(source, destination)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        if os.path.islink(source):
            link_target = os.readlink(source)
            if os.path.lexists(destination):
                os.unlink(destination)
            os.symlink(link_target, destination)
            os.unlink(source)
        else:
            shutil.copy2(source, destination)
            os.unlink(source)


class Downloader:
    def __init__(self):
        self.logger = logger

    def get_headers(self):
        headers = {}
        if CONFIG_MANAGER.get("dumb").get("github_token"):
            headers["Authorization"] = (
                f"token {CONFIG_MANAGER.get('dumb').get('github_token')}"
            )
        else:
            headers = {"Accept": "application/vnd.github.v3+json"}
        return headers

    def handle_rate_limits(self, response):
        if response.status_code in [403, 429]:
            wait_source = "default retry"
            if "Retry-After" in response.headers:
                retry_after = max(0, int(response.headers["Retry-After"]))
                wait_source = "Retry-After"
            elif "X-RateLimit-Reset" in response.headers:
                reset_time = int(response.headers["X-RateLimit-Reset"])
                current_time = time.time()
                retry_after = max(0, reset_time - current_time)
                wait_source = "X-RateLimit-Reset"
            else:
                retry_after = 60

            dumb_config = CONFIG_MANAGER.get("dumb") or {}
            max_wait = max(
                0,
                int(dumb_config.get("github_rate_limit_max_wait_seconds", 300)),
            )
            if retry_after > max_wait:
                self.logger.error(
                    "GitHub rate limit requires waiting %.1f seconds (%s), which "
                    "exceeds the configured %s-second maximum. Failing this fetch; "
                    "configure a GitHub token or retry after the quota resets.",
                    retry_after,
                    wait_source,
                    max_wait,
                )
                return False
            self.logger.warning(
                "Rate limit exceeded. Retrying after %.1f seconds.", retry_after
            )
            time.sleep(retry_after)
            return True
        return False

    def fetch_with_retries(self, url, headers, max_retries=5, accepted_statuses=(200,)):
        for attempt in range(max_retries):
            try:
                response = requests.get(url, headers=headers, timeout=(15, 300))
                if response.status_code in accepted_statuses:
                    return response
                if response.status_code in [403, 429]:
                    if not self.handle_rate_limits(response):
                        break
                else:
                    logger.info(f"Response status code: {response.status_code}")
                self.logger.info(
                    f"Retry attempt {attempt + 1} after rate limit handling."
                )
            except requests.RequestException as e:
                self.logger.error(f"Request error: {e}")
                time.sleep(2**attempt)
        self.logger.error(f"Failed to fetch {url} after {attempt + 1} attempts.")
        return None

    def get_ref_commit_sha(self, repo_owner, repo_name, ref):
        """Resolve a GitHub branch or tag ref to its underlying commit SHA."""
        try:
            encoded_ref = quote(str(ref or "").strip(), safe="")
            if not encoded_ref:
                return None, "GitHub ref is required."
            api_url = (
                f"https://api.github.com/repos/{repo_owner}/{repo_name}"
                f"/commits/{encoded_ref}"
            )
            response = self.fetch_with_retries(api_url, self.get_headers())
            if response and response.status_code == 200:
                data = response.json() if hasattr(response, "json") else {}
                sha = str((data or {}).get("sha") or "").strip().lower()
                if re.fullmatch(r"[0-9a-f]{40}", sha):
                    return sha, None
            status = response.status_code if response is not None else "no_response"
            return None, f"Unable to resolve GitHub ref commit SHA (status: {status})"
        except Exception as e:
            return None, f"Error resolving GitHub ref commit SHA: {e}"

    def download_release_version(
        self,
        process_name,
        key,
        repo_owner,
        repo_name,
        release_version,
        target_dir,
        zip_folder_name=None,
        exclude_dirs=None,
        staging_validator=None,
    ):
        try:
            logger.info(
                f"Downloading {process_name} release version: {release_version} from {repo_owner}/{repo_name}"
            )
            headers = self.get_headers()
            if release_version.lower() == "latest":
                release_version, error = self.get_latest_release(
                    repo_owner, repo_name, nightly=False
                )
                if error:
                    logger.error(error)
                    return False, error

            if key == "zurg":
                architecture = self.get_architecture()
                if CONFIG_MANAGER.get("dumb").get("github_token"):
                    logger.debug("Using GitHub token for downloading zurg.")
                    # repo_name = "zurg"
                    if "nightly" in release_version:
                        release_version, error = self.get_latest_release(
                            repo_owner, repo_name, nightly=True
                        )
                        if error:
                            logger.error(error)
                            return False, error

            elif key == "decypharr":
                architecture = self.get_architecture()

            elif key == "cli_debrid":
                architecture = None
                if release_version == "prerelease":
                    release_version, error = self.get_latest_release(
                        repo_owner, repo_name, nightly=False, prerelease=True
                    )
                    if error:
                        logger.error(error)
                        return False, error

            elif key == "emby":
                m = platform.machine().lower()
                if m in ("x86_64", "amd64"):
                    architecture = "amd64"
                elif m in ("aarch64", "arm64"):
                    architecture = "arm64"
                elif m in ("armv7l", "armhf"):
                    architecture = "armhf"
                else:
                    architecture = None

            elif key in [
                "sonarr",
                "radarr",
                "lidarr",
                "prowlarr",
                "readarr",
                "whisparr",
            ]:
                # Arr services use linux-x64, linux-arm64, linux-arm naming convention
                m = platform.machine().lower()
                libc_name = platform.libc_ver()[0].lower()
                is_musl = "musl" in libc_name or os.path.exists("/etc/alpine-release")
                if m in ("x86_64", "amd64"):
                    architecture = "linux-musl-x64" if is_musl else "linux-x64"
                elif m in ("aarch64", "arm64"):
                    architecture = "linux-musl-arm64" if is_musl else "linux-arm64"
                elif m in ("armv7l", "armhf"):
                    architecture = "linux-musl-arm" if is_musl else "linux-arm"
                else:
                    architecture = "linux-musl-x64" if is_musl else "linux-x64"
                # Arr releases extract to a folder named after the app (e.g., "Sonarr")
                zip_folder_name = key.capitalize()

            else:
                architecture = None

            # InfiniDysk is built from source, so its configured version may be a Git
            # tag such as "dev" without a corresponding GitHub Release object.
            if key == "infinidysk":
                encoded_version = quote(str(release_version), safe="")
                release_info = {
                    "tag_name": release_version,
                    "zipball_url": (
                        f"https://api.github.com/repos/{repo_owner}/{repo_name}"
                        f"/zipball/{encoded_version}"
                    ),
                }
            else:
                release_info, error = self.fetch_github_release_info(
                    repo_owner, repo_name, release_version, headers=None
                )
                if error:
                    logger.error(error)
                    return False, error

            # Bazarr's release asset is a flat bazarr.zip, not a GitHub source
            # archive wrapped in an owner-repository directory.
            if zip_folder_name is None and key != "bazarr":
                zip_folder_name = f"{repo_owner}-{repo_name}*"

            if key == "bazarr":
                bazarr_asset = next(
                    (
                        asset
                        for asset in release_info.get("assets", [])
                        if asset.get("name", "").lower() == "bazarr.zip"
                    ),
                    None,
                )
                download_url = (
                    bazarr_asset.get("browser_download_url") if bazarr_asset else None
                )
                asset_id = bazarr_asset.get("id") if bazarr_asset else None
            elif key == "emby":
                expected_name = (
                    f"emby-server-deb_{release_version}_{architecture}.deb".lower()
                )
                emby_asset = next(
                    (
                        asset
                        for asset in release_info.get("assets", [])
                        if asset.get("name", "").lower() == expected_name
                    ),
                    None,
                )
                download_url = (
                    emby_asset.get("browser_download_url") if emby_asset else None
                )
                asset_id = emby_asset.get("id") if emby_asset else None
            else:
                download_url, asset_id = self.find_asset_download_url(
                    release_info, architecture
                )
            if not download_url:
                return False, f"No release asset found for {process_name}."

            if asset_id:
                headers = self.get_headers()
                headers["Accept"] = "application/octet-stream"
                download_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/releases/assets/{asset_id}"

            logger.debug(
                f"Requesting {repo_name} release {release_version} from: {download_url}"
            )

            success, error = self.download_and_extract(
                download_url,
                target_dir,
                zip_folder_name,
                headers=headers,
                exclude_dirs=exclude_dirs,
                staging_validator=staging_validator,
            )
            if not success:
                logger.error(
                    f"Failed to download the {release_version} for {process_name}: {error}"
                )
                return False, error
            logger.info(f"Successfully downloaded {release_version} for {process_name}")
            return True, None
        except Exception as e:
            logger.error(f"Error in download release version: {e}")
            return False, str(e)

    def get_latest_release(
        self, repo_owner, repo_name, nightly=False, prerelease=False
    ):
        self.logger.debug(f"Fetching latest {repo_name} release.")
        headers = self.get_headers()
        if nightly or prerelease:
            api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/releases"
        else:
            api_url = (
                f"https://api.github.com/repos/{repo_owner}/{repo_name}/releases/latest"
            )
        response = self.fetch_with_retries(api_url, headers)
        if response and response.status_code == 200:
            releases = response.json()
            if nightly:
                nightly_releases = [
                    release
                    for release in releases
                    if isinstance(release, dict)
                    and "nightly" in str(release.get("tag_name") or "").lower()
                ]
                if nightly_releases:
                    latest_nightly = max(
                        nightly_releases, key=self._github_release_recency_key
                    )
                    return latest_nightly["tag_name"], None
                return None, "No nightly releases found."

            elif prerelease:
                if not isinstance(releases, list):
                    return None, "GitHub returned an invalid release list."
                prerelease_releases = [
                    release
                    for release in releases
                    if isinstance(release, dict)
                    and not release.get("draft")
                    and bool(release.get("prerelease"))
                ]
                if prerelease_releases:
                    latest_prerelease = max(
                        prerelease_releases, key=self._github_release_recency_key
                    )
                    return latest_prerelease["tag_name"], None
                return None, "No prerelease versions found."

            else:
                latest_release = response.json()
                version_tag = latest_release["tag_name"]
                self.logger.debug(f"{repo_name} latest release: {version_tag}")
                return version_tag, None

        else:
            return None, f"Error: Unable to access the {repo_name} repository API."

    @staticmethod
    def _normalized_release_tag(value):
        return str(value or "").strip().lower().removeprefix("v")

    def count_releases_behind(
        self,
        repo_owner,
        repo_name,
        current_version,
        latest_version,
        *,
        prerelease=False,
        nightly=False,
        max_pages=10,
    ):
        """Count published releases between an installed and available version.

        GitHub's release order is used instead of attempting to infer a distance
        from version-number components. ``None`` means the installed version was
        not found in the bounded release history (for example, a branch/dev
        build or a release older than the retained lookup window).
        """
        current_tag = self._normalized_release_tag(current_version)
        latest_tag = self._normalized_release_tag(latest_version)
        if not current_tag or not latest_tag:
            return None, "Current and latest release versions are required."
        if current_tag == latest_tag:
            return 0, None

        headers = self.get_headers()
        releases_seen = 0
        latest_seen = False
        for page in range(1, max(1, int(max_pages)) + 1):
            api_url = (
                f"https://api.github.com/repos/{repo_owner}/{repo_name}/releases"
                f"?per_page=100&page={page}"
            )
            response = self.fetch_with_retries(api_url, headers)
            if not response or response.status_code != 200:
                return None, "Unable to read GitHub release history."
            payload = response.json()
            if not isinstance(payload, list):
                return None, "GitHub returned an invalid release history."

            eligible = []
            for release in payload:
                if not isinstance(release, dict) or release.get("draft"):
                    continue
                tag_name = str(release.get("tag_name") or "").strip()
                if not tag_name:
                    continue
                if nightly:
                    if "nightly" not in tag_name.lower():
                        continue
                elif prerelease:
                    if not release.get("prerelease"):
                        continue
                elif release.get("prerelease"):
                    continue
                eligible.append(tag_name)

            for tag_name in eligible:
                normalized = self._normalized_release_tag(tag_name)
                if normalized == latest_tag:
                    latest_seen = True
                    releases_seen = 0
                    continue
                if not latest_seen:
                    continue
                releases_seen += 1
                if normalized == current_tag:
                    return releases_seen, None

            if len(payload) < 100:
                break

        return None, "Installed version was not found in GitHub release history."

    @staticmethod
    def _github_release_recency_key(release):
        return (
            str(release.get("published_at") or release.get("created_at") or ""),
            int(release.get("id") or 0),
            str(release.get("tag_name") or ""),
        )

    def get_branch(self, repo_owner, repo_name, branch, headers=None):
        headers = self.get_headers()
        zip_folder_name = f'{repo_name}-{branch.replace("/", "-").replace("--", "-")}'
        branch_url = f"https://github.com/{repo_owner}/{repo_name}/archive/refs/heads/{branch}.zip"
        self.logger.debug(f"Requesting {repo_name} release from {branch_url}")
        response = self.fetch_with_retries(branch_url, headers)

        if response and response.status_code == 200:
            return branch_url, zip_folder_name

        else:
            return None, f"Failed to get branch {branch} from {repo_name}."

    def get_commit(self, repo_owner, repo_name, commit_sha, headers=None):
        commit_sha = str(commit_sha or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
            return None, "Commit SHA must be a full 40-character hexadecimal value."

        headers = headers or self.get_headers()
        zip_folder_name = f"{repo_name}-{commit_sha}"
        commit_url = (
            f"https://github.com/{repo_owner}/{repo_name}/archive/{commit_sha}.zip"
        )
        self.logger.debug(
            "Requesting %s commit %s from %s",
            repo_name,
            commit_sha,
            commit_url,
        )
        response = self.fetch_with_retries(commit_url, headers)

        if response and response.status_code == 200:
            return commit_url, zip_folder_name

        return None, f"Failed to get commit {commit_sha} from {repo_name}."

    def fetch_github_release_info(
        self, repo_owner, repo_name, release_version, headers=None
    ):
        headers = self.get_headers()
        api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/releases/tags/{release_version}"
        self.logger.debug(f"Fetching release information from {api_url}")
        response = self.fetch_with_retries(api_url, headers)

        if response and response.status_code == 200:
            return response.json(), None

        else:
            return None, f"Failed to get {repo_name} release assets."

    def find_asset_download_url(self, release_info, architecture=None):
        assets = release_info.get("assets", [])
        self.logger.debug(
            f"Found {len(assets)} assets for the release: {release_info.get('tag_name')}"
        )
        self.logger.debug(f"Architecture requested: {architecture}")
        if architecture:
            normalized_arch = self.normalize_arch(architecture)
            arch_parts = normalized_arch.split("_")
            want_musl = "musl" in normalized_arch or "musl" in str(architecture).lower()
            self.logger.debug(
                f"Normalized architecture: {normalized_arch}, parts: {arch_parts}"
            )
            self.logger.debug(
                f"Searching for assets matching architecture: {architecture}"
            )

            def _matches_parts(name, parts):
                return all(part in name for part in parts if part)

            alt_parts = []
            if "x64" in arch_parts:
                alt_parts = ["linux", "amd64"] if "linux" in arch_parts else ["amd64"]
            elif "arm64" in arch_parts:
                alt_parts = (
                    ["linux", "aarch64"] if "linux" in arch_parts else ["aarch64"]
                )

            def _scan_assets(mode: str):
                for asset in assets:
                    name = asset["name"].lower()
                    self.logger.debug(f"Checking asset: {name}")
                    if mode == "exclude_musl" and "musl" in name:
                        continue
                    if mode == "only_musl" and "musl" not in name:
                        continue

                    if architecture and architecture in asset["name"]:
                        self.logger.debug(
                            f"Assets ID found: {asset['id']} for architecture: {architecture}"
                        )
                        self.logger.debug(
                            f"Browser Download URL: {asset['browser_download_url']}"
                        )
                        return asset["browser_download_url"], asset["id"]

                    if normalized_arch in name:
                        self.logger.debug(
                            f"Assets ID found: {asset['id']} for architecture: {architecture}"
                        )
                        self.logger.debug(
                            f"Browser Download URL: {asset['browser_download_url']}"
                        )
                        return asset["browser_download_url"], asset["id"]

                    if _matches_parts(name, arch_parts):
                        self.logger.debug(
                            f"Assets ID found: {asset['id']} for architecture: {architecture}"
                        )
                        self.logger.debug(
                            f"Browser Download URL: {asset['browser_download_url']}"
                        )
                        return asset["browser_download_url"], asset["id"]

                    if alt_parts and _matches_parts(name, alt_parts):
                        self.logger.debug(
                            f"Assets ID found: {asset['id']} for architecture: {architecture}"
                        )
                        self.logger.debug(
                            f"Browser Download URL: {asset['browser_download_url']}"
                        )
                        return asset["browser_download_url"], asset["id"]
                return None, None

            # First pass: honor musl preference
            url, asset_id = _scan_assets("only_musl" if want_musl else "exclude_musl")
            if url:
                return url, asset_id
            # Second pass: relax musl constraint
            url, asset_id = _scan_assets("any")
            if url:
                return url, asset_id

            if assets:
                # Prefer a linux asset if linux was requested, instead of arbitrary fallback
                linux_assets = [
                    asset for asset in assets if "linux" in asset["name"].lower()
                ]
                if linux_assets:
                    asset = linux_assets[0]
                    self.logger.warning(
                        "No exact architecture match for %s. Falling back to linux asset: %s",
                        architecture,
                        asset["name"],
                    )
                    self.logger.debug(f"Download URL: {asset['browser_download_url']}")
                    return asset["browser_download_url"], asset["id"]

                self.logger.warning(
                    "No matching asset found for architecture: %s. Falling back to the first available asset.",
                    architecture,
                )
                self.logger.debug(f"Download URL: {assets[0]['browser_download_url']}")
                return assets[0]["browser_download_url"], assets[0]["id"]

        zipball_url = release_info.get("zipball_url")
        tarball_url = release_info.get("tarball_url")

        if zipball_url:
            self.logger.debug("No assets found. Using zipball_url.")
            return zipball_url, None

        if tarball_url:
            self.logger.debug("No assets found. Using tarball_url.")
            return tarball_url, None

        self.logger.error("No assets or zipball/tarball URL found for the release.")
        return None, None

    def _safe_extract_path(self, target_dir, member_name):
        target_real = os.path.realpath(target_dir)
        destination = os.path.realpath(os.path.join(target_dir, member_name))
        try:
            if os.path.commonpath([target_real, destination]) != target_real:
                self.logger.warning(
                    "Skipping archive member outside target directory: %s",
                    member_name,
                )
                return None
        except Exception:
            self.logger.warning(
                "Skipping archive member with invalid target path: %s", member_name
            )
            return None
        return destination

    @staticmethod
    def _archive_limits():
        install_cache = (CONFIG_MANAGER.get("dumb") or {}).get("install_cache") or {}
        return {
            "download_bytes": int(
                float(install_cache.get("max_download_size_mb", 4096)) * 1024 * 1024
            ),
            "entries": int(install_cache.get("max_archive_entries", 250000)),
            "unpacked_bytes": int(
                float(install_cache.get("max_unpacked_size_gib", 50))
                * 1024
                * 1024
                * 1024
            ),
        }

    @staticmethod
    def _strict_relative_path(member_name, zip_folder_name=None, single=False):
        normalized = str(member_name or "").replace("\\", "/")
        if normalized.startswith("/"):
            raise ValueError(f"Unsafe archive member path: {member_name}")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized:
            return None
        if zip_folder_name:
            raw_patterns = (
                zip_folder_name
                if isinstance(zip_folder_name, (list, tuple))
                else [zip_folder_name]
            )
            archive_root_patterns = [
                str(value).replace("\\", "/").rstrip("/")
                for value in raw_patterns
                if str(value).strip()
            ]
            member_root, separator, remainder = normalized.partition("/")
            if any(
                fnmatch.fnmatchcase(member_root, pattern)
                for pattern in archive_root_patterns
            ):
                normalized = remainder if separator else member_root
            elif not single:
                return None
        relative = os.path.normpath(normalized)
        if (
            not relative
            or relative in {".", ".."}
            or os.path.isabs(relative)
            or relative.startswith(f"..{os.sep}")
            or "\x00" in relative
        ):
            raise ValueError(f"Unsafe archive member path: {member_name}")
        return relative

    @staticmethod
    def _normalized_archive_excludes(target_dir, exclude_dirs=None):
        target = Path(os.path.abspath(target_dir))
        resolved_target = target.resolve()
        normalized = set()
        for value in exclude_dirs or []:
            candidate = Path(str(value))
            if candidate.is_absolute():
                try:
                    candidate = Path(os.path.abspath(candidate)).relative_to(target)
                except ValueError:
                    try:
                        candidate = candidate.resolve().relative_to(resolved_target)
                    except ValueError:
                        # An absolute exclusion outside this install root cannot
                        # match an archive-relative member safely.
                        continue
            text = str(candidate).replace("\\", "/").strip("/")
            if text and text not in {".", ".."} and ".." not in Path(text).parts:
                normalized.add(text)
        return normalized

    def _merge_staging_transactionally(self, staging_dir, target_dir):
        staged_file_count = sum(
            len(filenames) for _, _, filenames in os.walk(staging_dir)
        )
        if staged_file_count == 0:
            return False, "Archive extraction produced no eligible files."

        # Service roots such as /decypharr are symlinks into /data. Resolve the
        # live target before choosing the backup directory so every os.replace
        # remains on the installation filesystem without replacing the symlink.
        target = os.path.realpath(os.path.abspath(target_dir))
        parent = os.path.dirname(target)
        os.makedirs(target, exist_ok=True)
        backup = tempfile.mkdtemp(
            prefix=f".{os.path.basename(target)}.dumb-overlay-backup-", dir=parent
        )
        applied = []
        created_dirs = []
        try:
            for current_root, directories, filenames in os.walk(staging_dir):
                directories.sort()
                filenames.sort()
                relative_root = os.path.relpath(current_root, staging_dir)
                destination_root = (
                    target
                    if relative_root == "."
                    else os.path.join(target, relative_root)
                )
                if not os.path.isdir(destination_root):
                    if os.path.lexists(destination_root):
                        previous_directory_entry = os.path.join(backup, relative_root)
                        os.makedirs(
                            os.path.dirname(previous_directory_entry), exist_ok=True
                        )
                        os.replace(destination_root, previous_directory_entry)
                        applied.append((destination_root, previous_directory_entry))
                    os.makedirs(destination_root, exist_ok=True)
                    created_dirs.append(destination_root)
                for filename in filenames:
                    source = os.path.join(current_root, filename)
                    relative = os.path.relpath(source, staging_dir)
                    destination = self._safe_extract_path(target, relative)
                    if not destination:
                        raise ValueError(f"Unsafe staged path: {relative}")
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    previous = None
                    if os.path.lexists(destination):
                        previous = os.path.join(backup, relative)
                        os.makedirs(os.path.dirname(previous), exist_ok=True)
                        os.replace(destination, previous)
                    _replace_cross_device_safe(source, destination)
                    applied.append((destination, previous))
            shutil.rmtree(backup)
            return True, None
        except Exception as error:
            for destination, previous in reversed(applied):
                try:
                    if os.path.lexists(destination):
                        if os.path.isdir(destination) and not os.path.islink(
                            destination
                        ):
                            shutil.rmtree(destination)
                        else:
                            os.unlink(destination)
                    if previous and os.path.lexists(previous):
                        os.makedirs(os.path.dirname(destination), exist_ok=True)
                        os.replace(previous, destination)
                except OSError:
                    pass
            for directory in reversed(created_dirs):
                try:
                    os.rmdir(directory)
                except OSError:
                    pass
            shutil.rmtree(backup, ignore_errors=True)
            return False, f"Failed applying extracted files: {error}"

    def _extract_tarfile(
        self, tar_bytes_io, target_dir, zip_folder_name=None, exclude_dirs=None
    ):
        limits = self._archive_limits()
        try:
            with tarfile.open(fileobj=tar_bytes_io, mode="r:*") as tar:
                members = tar.getmembers()
                if len(members) > limits["entries"]:
                    raise ValueError("Archive contains too many entries.")
                total_size = sum(member.size for member in members if member.isfile())
                if total_size > limits["unpacked_bytes"]:
                    raise ValueError("Archive expands beyond the configured limit.")

                eligible_members = []
                regular_paths = set()
                for member in members:
                    self.logger.debug(
                        f"Found tar member: {member.name} ({member.size} bytes)"
                    )
                    if member.isdev():
                        raise ValueError(
                            f"Archive contains unsupported link/device entry: {member.name}"
                        )
                    if (
                        not member.isfile()
                        and not member.issym()
                        and not member.islnk()
                    ):
                        continue

                    member_name = self._strict_relative_path(
                        member.name,
                        zip_folder_name,
                        single=len(members) == 1,
                    )
                    if member_name is None:
                        continue

                    normalized_excludes = self._normalized_archive_excludes(
                        target_dir, exclude_dirs
                    )
                    if any(
                        member_name == excluded
                        or member_name.startswith(f"{excluded}/")
                        for excluded in normalized_excludes
                    ):
                        continue

                    eligible_members.append((member, member_name))
                    if member.isfile():
                        regular_paths.add(member_name)

                regular_members = {
                    member_name: member
                    for member, member_name in eligible_members
                    if member.isfile()
                }
                expanded_hardlink_bytes = 0
                safe_hardlink_paths = set()
                for member, member_name in eligible_members:
                    if not member.islnk():
                        continue
                    linked_name = self._strict_relative_path(
                        member.linkname,
                        zip_folder_name,
                    )
                    if linked_name is None:
                        linked_name = os.path.normpath(
                            os.path.join(os.path.dirname(member_name), member.linkname)
                        )
                    linked_member = regular_members.get(linked_name)
                    if linked_member is None:
                        raise ValueError(
                            "Archive hard link does not target an internal regular "
                            f"file: {member.name}"
                        )
                    expanded_hardlink_bytes += linked_member.size
                    safe_hardlink_paths.add(member_name)
                if total_size + expanded_hardlink_bytes > limits["unpacked_bytes"]:
                    raise ValueError("Archive expands beyond the configured limit.")
                safe_file_paths = regular_paths | safe_hardlink_paths

                for member, member_name in eligible_members:
                    fpath = self._safe_extract_path(target_dir, member_name)
                    if not fpath:
                        raise ValueError(f"Unsafe archive member: {member_name}")

                    os.makedirs(os.path.dirname(fpath), exist_ok=True)
                    if member.issym():
                        link_target = str(member.linkname or "")
                        if (
                            not link_target
                            or len(link_target.encode("utf-8")) > 4096
                            or "\x00" in link_target
                            or "\\" in link_target
                            or os.path.isabs(link_target)
                        ):
                            raise ValueError(
                                f"Archive contains unsafe symlink: {member.name}"
                            )
                        resolved_link = os.path.normpath(
                            os.path.join(os.path.dirname(member_name), link_target)
                        )
                        if (
                            resolved_link in {".", ".."}
                            or resolved_link.startswith(f"..{os.sep}")
                            or resolved_link not in safe_file_paths
                            or any(
                                path.startswith(f"{member_name}/")
                                for path in safe_file_paths
                            )
                        ):
                            raise ValueError(
                                "Archive symlink does not target an internal regular "
                                f"file: {member.name}"
                            )
                        os.symlink(link_target, fpath)
                        continue

                    linked_member = None
                    if member.islnk():
                        linked_name = self._strict_relative_path(
                            member.linkname,
                            zip_folder_name,
                        )
                        if linked_name is None:
                            linked_name = os.path.normpath(
                                os.path.join(
                                    os.path.dirname(member_name), member.linkname
                                )
                            )
                        linked_member = regular_members[linked_name]

                    file_obj = tar.extractfile(linked_member or member)
                    if not file_obj:
                        continue

                    if member_name.endswith(".tar"):
                        self.logger.debug(
                            f"Found nested TAR: {member_name}, extracting inline..."
                        )
                        file_obj = tar.extractfile(member)
                        if not file_obj:
                            self.logger.error(
                                f"Could not open nested tar: {member_name}"
                            )
                            raise ValueError(
                                f"Could not open nested tar: {member_name}"
                            )
                        nested_tar_data = file_obj.read()
                        self.logger.debug(
                            f"Nested TAR {member_name} size: {len(nested_tar_data)} bytes"
                        )
                        self._extract_tarfile(
                            io.BytesIO(nested_tar_data),
                            target_dir,
                            zip_folder_name,
                            exclude_dirs,
                        )
                        continue

                    with open(fpath, "wb") as dst:
                        shutil.copyfileobj(file_obj, dst)
                    os.chmod(fpath, (linked_member or member).mode & 0o777)

        except tarfile.TarError as e:
            self.logger.error(f"Failed to extract TAR file: {e}")
            raise

    def download_and_extract(
        self,
        url,
        target_dir,
        zip_folder_name=None,
        headers=None,
        exclude_dirs=None,
        expected_sha256=None,
        staging_validator=None,
    ):
        staging_dir = None
        try:
            self.logger.debug(f"Downloading from {url}")
            headers = dict(headers or self.get_headers())
            cached_content, cached_metadata = INSTALL_CACHE.lookup_download(url)
            if cached_metadata.get("etag"):
                headers["If-None-Match"] = cached_metadata["etag"]
            if cached_metadata.get("last_modified"):
                headers["If-Modified-Since"] = cached_metadata["last_modified"]
            response = self.fetch_with_retries(
                url, headers, accepted_statuses=(200, 304)
            )
            limits = self._archive_limits()

            cached_fallback = response is None and cached_content is not None
            if response is None and not cached_fallback:
                return False, "Failed to download."
            if response is not None and response.status_code not in (200, 304):
                return False, "Failed to download."

            if cached_fallback:
                self.logger.warning(
                    "Download revalidation failed; using the last digest-verified cache entry for %s.",
                    url,
                )
                content = cached_content
            elif response.status_code == 304:
                if cached_content is None:
                    return (
                        False,
                        "Download cache revalidation returned no cached object.",
                    )
                content = cached_content
            else:
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > limits["download_bytes"]:
                            return False, "Download exceeds the configured size limit."
                    except (TypeError, ValueError):
                        pass
                content = response.content
            size = len(content)
            if size > limits["download_bytes"]:
                return False, "Download exceeds the configured size limit."
            expected_digest = str(expected_sha256 or "").strip().lower()
            if expected_digest.startswith("sha256:"):
                expected_digest = expected_digest.removeprefix("sha256:")
            if expected_digest:
                if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
                    return False, "Expected SHA-256 digest is invalid."
                actual_digest = hashlib.sha256(content).hexdigest()
                if actual_digest != expected_digest:
                    INSTALL_CACHE.invalidate_download(url, "digest-mismatch")
                    return (
                        False,
                        "Downloaded archive failed its published SHA-256 verification.",
                    )
            self.logger.debug(
                f"{zip_folder_name} download successful. Content size: {size} bytes"
            )

            response_headers = response.headers if response is not None else {}
            cd = response_headers.get("Content-Disposition", "")
            m = re.search(r'filename="?([^"]+)"?', cd)
            if m:
                filename = m.group(1)
            else:
                filename = cached_metadata.get("filename") or (
                    url.split("?")[0].rstrip("/").split("/")[-1] or "download.bin"
                )

            lower_name = filename.lower()
            ext = lower_name.rsplit(".", 1)[-1] if "." in lower_name else ""
            ctype = (response_headers.get("Content-Type") or "").lower()
            if not ctype:
                ctype = str(cached_metadata.get("content_type") or "").lower()

            if response is not None and response.status_code == 200:
                INSTALL_CACHE.store_download(
                    url,
                    content,
                    etag=response_headers.get("ETag"),
                    last_modified=response_headers.get("Last-Modified"),
                    filename=filename,
                    content_type=ctype,
                )

            def looks_like_zip():
                if ext in {"zip"}:
                    return True
                return any(
                    t in ctype
                    for t in ("application/zip", "application/x-zip-compressed")
                )

            def looks_like_tar():
                if ext in {"tar", "tgz", "gz", "bz2", "xz", "txz", "tbz2"}:
                    return True
                return any(
                    t in ctype
                    for t in (
                        "application/x-tar",
                        "application/x-gtar",
                        "application/x-7z-compressed",
                        "application/gzip",
                        "application/x-gzip",
                        "application/x-bzip2",
                        "application/x-xz",
                    )
                )

            archive_data = io.BytesIO(content)
            normalized_excludes = self._normalized_archive_excludes(
                target_dir, exclude_dirs
            )
            resolved_target = os.path.realpath(os.path.abspath(target_dir))
            target_parent = os.path.dirname(resolved_target)
            os.makedirs(target_parent, exist_ok=True)
            staging_dir = tempfile.mkdtemp(
                prefix=f".{os.path.basename(resolved_target)}.dumb-extract-",
                dir=target_parent,
            )

            if looks_like_zip():
                try:
                    with zipfile.ZipFile(io.BytesIO(content)) as z:
                        entries = z.infolist()
                        if len(entries) > limits["entries"]:
                            raise ValueError("Archive contains too many entries.")
                        if (
                            sum(entry.file_size for entry in entries)
                            > limits["unpacked_bytes"]
                        ):
                            raise ValueError(
                                "Archive expands beyond the configured limit."
                            )
                        self.logger.debug(
                            f"Extracting {zip_folder_name or filename} to staging"
                        )
                        eligible_entries = []
                        regular_paths = set()
                        for file_info in entries:
                            unix_mode = (file_info.external_attr >> 16) & 0xFFFF
                            if file_info.is_dir():
                                continue
                            relative = self._strict_relative_path(
                                file_info.filename,
                                zip_folder_name,
                                single=len(entries) == 1,
                            )
                            if relative is None:
                                continue
                            if any(
                                relative == excluded
                                or relative.startswith(f"{excluded}/")
                                for excluded in normalized_excludes
                            ):
                                continue
                            is_symlink = stat.S_ISLNK(unix_mode)
                            eligible_entries.append((file_info, relative, is_symlink))
                            if not is_symlink:
                                regular_paths.add(relative)

                        for file_info, relative, is_symlink in eligible_entries:
                            fpath = self._safe_extract_path(staging_dir, relative)
                            if not fpath:
                                raise ValueError(
                                    f"Unsafe archive member: {file_info.filename}"
                                )
                            os.makedirs(os.path.dirname(fpath), exist_ok=True)
                            if is_symlink:
                                raw_target = z.read(file_info)
                                if len(raw_target) > 4096:
                                    raise ValueError(
                                        f"Archive symlink target is too long: {file_info.filename}"
                                    )
                                link_target = raw_target.decode(
                                    "utf-8", errors="strict"
                                )
                                if (
                                    not link_target
                                    or "\x00" in link_target
                                    or "\\" in link_target
                                    or os.path.isabs(link_target)
                                ):
                                    raise ValueError(
                                        f"Archive contains unsafe symlink: {file_info.filename}"
                                    )
                                resolved_link = os.path.normpath(
                                    os.path.join(os.path.dirname(relative), link_target)
                                )
                                if (
                                    resolved_link in {".", ".."}
                                    or resolved_link.startswith(f"..{os.sep}")
                                    or resolved_link not in regular_paths
                                ):
                                    raise ValueError(
                                        "Archive symlink does not target an internal "
                                        f"regular file: {file_info.filename}"
                                    )
                                os.symlink(link_target, fpath)
                                continue
                            with (
                                open(fpath, "wb") as dst,
                                z.open(file_info, "r") as src,
                            ):
                                shutil.copyfileobj(src, dst)
                    if staging_validator is not None:
                        valid, validation_error = staging_validator(staging_dir)
                        if not valid:
                            return (
                                False,
                                "Staged archive validation failed: "
                                f"{validation_error or 'unknown validation error'}",
                            )
                    success, error = self._merge_staging_transactionally(
                        staging_dir, target_dir
                    )
                    if not success:
                        return False, error
                    self.logger.debug(f"Successfully extracted ZIP to {target_dir}")
                    return True, None
                except (zipfile.BadZipFile, ValueError, OSError) as error:
                    INSTALL_CACHE.invalidate_download(url, "invalid-zip")
                    return False, f"Invalid ZIP archive: {error}"

            if looks_like_tar():
                try:
                    self.logger.debug("Attempting TAR extraction...")
                    self._extract_tarfile(
                        archive_data,
                        staging_dir,
                        zip_folder_name,
                        normalized_excludes,
                    )
                    if staging_validator is not None:
                        valid, validation_error = staging_validator(staging_dir)
                        if not valid:
                            return (
                                False,
                                "Staged archive validation failed: "
                                f"{validation_error or 'unknown validation error'}",
                            )
                    success, error = self._merge_staging_transactionally(
                        staging_dir, target_dir
                    )
                    if not success:
                        return False, error
                    self.logger.debug(f"Successfully extracted TAR to {target_dir}")
                    return True, None
                except (tarfile.TarError, ValueError, OSError) as error:
                    INSTALL_CACHE.invalidate_download(url, "invalid-tar")
                    return False, f"Invalid TAR archive: {error}"

            os.makedirs(target_dir, exist_ok=True)
            out_path = self._safe_extract_path(target_dir, filename)
            if not out_path:
                return False, "Unsafe output path."
            temporary_out = os.path.join(
                target_dir, f".{os.path.basename(out_path)}.{os.getpid()}.part"
            )
            with open(temporary_out, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary_out, out_path)
            self.logger.debug(f"Saved non-archive asset to {out_path}")
            return True, None

        except Exception as e:
            self.logger.error(f"Error in download and extraction: {e}")
            return False, str(e)
        finally:
            if staging_dir:
                shutil.rmtree(staging_dir, ignore_errors=True)

    def set_permissions(self, file_path, mode):
        try:
            os.chmod(file_path, mode)
            self.logger.debug(f"Set permissions for {file_path} to {oct(mode)}")
        except Exception as e:
            self.logger.error(f"Failed to set permissions for {file_path}: {e}")

    def get_architecture(self):
        try:
            arch_map = {
                ("AMD64", "Windows"): "windows-amd64",
                ("AMD64", "Linux"): "linux-amd64",
                ("AMD64", "Darwin"): "darwin-amd64",
                ("x86_64", "Linux"): "linux-amd64",
                ("x86_64", "Darwin"): "darwin-amd64",
                ("arm64", "Linux"): "linux-arm64",
                ("arm64", "Darwin"): "darwin-arm64",
                ("aarch64", "Linux"): "linux-arm64",
                ("mips64", "Linux"): "linux-mips64",
                ("mips64le", "Linux"): "linux-mips64le",
                ("ppc64le", "Linux"): "linux-ppc64le",
                ("riscv64", "Linux"): "linux-riscv64",
                ("s390x", "Linux"): "linux-s390x",
            }
            system_arch = platform.machine()
            system_os = platform.system()
            self.logger.debug(
                "System Architecture: %s, Operating System: %s", system_arch, system_os
            )
            return arch_map.get((system_arch, system_os), "unknown")
        except Exception as e:
            self.logger.error(f"Error determining system architecture: {e}")
            return "unknown"

    @staticmethod
    def normalize_arch(arch):
        arch = arch.lower().replace("-", "_")
        full_replacements = {
            "linux_amd64": "linux_x86_64",
            "darwin_amd64": "darwin_x86_64",
            "windows_amd64": "windows_x86_64",
            "linux_aarch64": "linux_arm64",
        }
        return full_replacements.get(arch, arch)
