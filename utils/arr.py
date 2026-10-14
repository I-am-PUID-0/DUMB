from utils.global_logger import logger
import platform, subprocess, os, re, requests, json, time, shutil, tarfile, stat
from utils.download import Downloader
from utils.transactional_install import DirectoryReleaseTransaction
from utils.whisparr_settings import (
    WhisparrConfigurationError,
    normalize_whisparr_release_version,
    legacy_whisparr_database,
    read_whisparr_version,
    resolve_whisparr_version,
    validate_whisparr_runtime,
    whisparr_release_selector,
    whisparr_repository,
)


class ArrInstaller:
    def __init__(
        self,
        app_name: str,
        version: str = "4",
        branch: str = "main",
        install_dir=None,
        repo_owner: str = None,
        repo_name: str = None,
        *,
        whisparr_config: dict | None = None,
    ):
        self.logger = logger
        self.app_name = app_name.lower()
        self.app_name_cap = app_name.capitalize()
        self.version = version
        self.branch = branch
        self.install_dir = install_dir or f"/opt/{self.app_name}"
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.whisparr_config = None
        self._whisparr_release_tag = None
        if self.app_name in {"whisparr", "whisparr-v3"}:
            legacy_v3 = self.app_name == "whisparr-v3"
            self.app_name = "whisparr"
            self.app_name_cap = "Whisparr"
            self.whisparr_config = dict(whisparr_config or {})
            if whisparr_config is None:
                self.whisparr_config.update(
                    {
                        "whisparr_version": "v3" if legacy_v3 else "",
                        "branch": branch,
                        "repo_owner": repo_owner,
                        "repo_name": repo_name,
                        "pinned_version": version if version not in {"3", "4"} else "",
                    }
                )
            if install_dir is None:
                self.install_dir = "/opt/whisparr"

    def _resolve_whisparr_release_tag(self):
        config = self.whisparr_config
        flavor = resolve_whisparr_version(config)
        selector = whisparr_release_selector(config)
        if self._whisparr_release_tag:
            tag = self._whisparr_release_tag
        else:
            owner, repo = whisparr_repository(config)
            downloader = Downloader()
            if selector in {"latest", "prerelease"}:
                tag, error = downloader.get_latest_release(
                    owner, repo, prerelease=selector == "prerelease"
                )
                if not tag:
                    raise WhisparrConfigurationError(
                        error or "Failed to resolve the selected Whisparr release."
                    )
            else:
                info, _ = downloader.fetch_github_release_info(owner, repo, selector)
                if info:
                    tag = info.get("tag_name")
                else:
                    requested_version = normalize_whisparr_release_version(selector)
                    matches = set()
                    headers = downloader.get_headers()
                    for page in range(1, 6):
                        url = (
                            f"https://api.github.com/repos/{owner}/{repo}/releases"
                            f"?per_page=100&page={page}"
                        )
                        response = downloader.fetch_with_retries(url, headers)
                        if not response or response.status_code != 200:
                            raise WhisparrConfigurationError(
                                "Failed to read Whisparr release history for the configured version."
                            )
                        releases = response.json()
                        if not isinstance(releases, list):
                            raise WhisparrConfigurationError(
                                "Whisparr release history is invalid."
                            )
                        for release in releases:
                            if not isinstance(release, dict) or release.get("draft"):
                                continue
                            candidate = release.get("tag_name")
                            if (
                                requested_version
                                and normalize_whisparr_release_version(candidate)
                                == requested_version
                            ):
                                matches.add(candidate)
                        if len(releases) < 100:
                            break
                    if len(matches) != 1:
                        raise WhisparrConfigurationError(
                            "The configured Whisparr version has no unique published release; "
                            "use an exact release tag."
                        )
                    tag = matches.pop()
            self._whisparr_release_tag = tag
        version = normalize_whisparr_release_version(tag)
        if not version or version.split(".", 1)[0] != flavor[1:]:
            raise WhisparrConfigurationError(
                f"Published Whisparr release {tag!r} does not match selected {flavor}."
            )
        return tag

    @staticmethod
    def _find_whisparr_binary(install_dir):
        for relative in (os.path.join("Whisparr", "Whisparr"), "Whisparr"):
            path = os.path.join(install_dir, relative)
            if os.path.isfile(path):
                return path
        return None

    def _prepare_whisparr_transaction(self):
        parent = os.path.dirname(os.path.realpath(self.install_dir))
        os.makedirs(parent, exist_ok=True)
        checked_parent = parent
        while True:
            parent_info = os.stat(checked_parent)
            sticky_system_tmp = parent_info.st_uid == 0 and bool(
                parent_info.st_mode & stat.S_ISVTX
            )
            if parent_info.st_uid not in {0, os.geteuid()} or (
                stat.S_IMODE(parent_info.st_mode) & 0o022 and not sticky_system_tmp
            ):
                raise WhisparrConfigurationError(
                    "Whisparr installation requires controller-owned parent directories "
                    "without group or other write permission."
                )
            next_parent = os.path.dirname(checked_parent)
            if next_parent == checked_parent:
                break
            checked_parent = next_parent
        transaction = DirectoryReleaseTransaction(self.install_dir, "Whisparr")
        candidate = transaction.prepare()
        os.chmod(candidate, 0o700)
        return transaction, candidate

    def _adopt_legacy_whisparr(self, before_activate=None):
        config = self.whisparr_config
        command = config.get("command") or []
        source_binary = None
        if isinstance(command, list):
            for argument in command[:2]:
                if (
                    isinstance(argument, str)
                    and os.path.basename(argument) == "Whisparr"
                    and os.path.isfile(argument)
                ):
                    source_binary = argument
                    break
        if source_binary is None and config.get("install_dir"):
            source_binary = self._find_whisparr_binary(config["install_dir"])
        if not source_binary:
            return False
        validate_whisparr_runtime(config, source_binary)
        source_dir = os.path.realpath(os.path.dirname(source_binary))
        data_dir = os.path.realpath(config["config_dir"])
        target_dir = os.path.realpath(self.install_dir)
        family = resolve_whisparr_version(config)
        saved_profile = (config.get("whisparr_profiles") or {}).get(family, {})
        managed_flat_runtime = (
            saved_profile.get("initialized")
            and os.path.dirname(target_dir) == source_dir
            and os.path.basename(target_dir) == family
        )
        data_overlap = os.path.commonpath((source_dir, data_dir)) in {
            source_dir,
            data_dir,
        }
        target_overlap = os.path.commonpath((source_dir, target_dir)) in {
            source_dir,
            target_dir,
        }
        if data_overlap or (target_overlap and not managed_flat_runtime):
            raise WhisparrConfigurationError(
                "Cannot adopt a Whisparr runtime whose folder overlaps its data or managed binary directory."
            )
        database_ignore = shutil.ignore_patterns(
            "whisparr*.db", "whisparr*.db-*", "logs.db", "logs.db-*"
        )

        def ignored_managed_entry(name):
            return name in {"v2", "v3"} or name.startswith((".v2.dumb-", ".v3.dumb-"))

        def ignore_runtime(current, names):
            ignored = database_ignore(current, names)
            if managed_flat_runtime and os.path.realpath(current) == source_dir:
                ignored.update(name for name in names if ignored_managed_entry(name))
            return ignored

        for current, directories, files in os.walk(source_dir):
            if managed_flat_runtime and os.path.realpath(current) == source_dir:
                # Exclusions must never turn a directory symlink into an
                # accepted source. In particular, a managed name cannot alias
                # another runtime or an unrelated directory.
                for name in directories + files:
                    if ignored_managed_entry(name) and os.path.islink(
                        os.path.join(current, name)
                    ):
                        raise WhisparrConfigurationError(
                            "Cannot adopt a Whisparr runtime containing symbolic-link directories."
                        )
                directories[:] = [
                    name for name in directories if not ignored_managed_entry(name)
                ]
                files = [name for name in files if not ignored_managed_entry(name)]
            for name in directories + files:
                path = os.path.join(current, name)
                if os.path.islink(path) and name in directories:
                    raise WhisparrConfigurationError(
                        "Cannot adopt a Whisparr runtime containing symbolic-link directories."
                    )
                if (
                    os.path.islink(path)
                    and os.path.commonpath((os.path.realpath(path), source_dir))
                    != source_dir
                ):
                    raise WhisparrConfigurationError(
                        "Cannot adopt a Whisparr runtime containing external symbolic links."
                    )
        transaction, candidate = self._prepare_whisparr_transaction()
        try:
            transaction.mark_building()
            shutil.copytree(
                source_dir,
                os.path.join(candidate, "Whisparr"),
                ignore=ignore_runtime,
            )
            binary = os.path.join(candidate, "Whisparr", "Whisparr")
            validate_whisparr_runtime(config, binary)
            if read_whisparr_version(binary) != read_whisparr_version(source_binary):
                raise WhisparrConfigurationError(
                    "Whisparr runtime changed while preparing its legacy installation."
                )
            os.chmod(binary, 0o755)
            os.chmod(candidate, 0o755)
            if before_activate is not None:
                before_activate()
            transaction.activate()
            transaction.commit()
            return True
        except Exception:
            if transaction.activated:
                transaction.rollback()
            raise
        finally:
            if not transaction.activated:
                transaction.abandon()

    def _install_whisparr(self, force=False, before_activate=None):
        config = self.whisparr_config
        resolve_whisparr_version(config)
        selector = whisparr_release_selector(config)
        if config.get("config_dir"):
            data_dir = os.path.realpath(config["config_dir"])
            binary_dir = os.path.realpath(self.install_dir)
            if os.path.commonpath((data_dir, binary_dir)) in {data_dir, binary_dir}:
                raise WhisparrConfigurationError(
                    "Whisparr data and managed binary directories must be separate "
                    "and must not contain one another."
                )
        arch = platform.machine().lower()
        if arch not in {"x86_64", "amd64", "aarch64", "arm64", "armv7l", "armhf"}:
            raise WhisparrConfigurationError(f"Unsupported architecture: {arch}")
        existing_binary = self._find_whisparr_binary(self.install_dir)
        legacy_moving_install = (
            not force
            and selector in {"latest", "prerelease"}
            and legacy_whisparr_database(config)
        )
        saved_profile = (config.get("whisparr_profiles") or {}).get(
            resolve_whisparr_version(config), {}
        )
        preserve_saved_runtime = (
            not force and saved_profile.get("initialized") and not existing_binary
        )
        if preserve_saved_runtime:
            command = config.get("command") or []
            source_binary = next(
                (
                    argument
                    for argument in command[:2]
                    if isinstance(argument, str)
                    and os.path.basename(argument) == "Whisparr"
                    and os.path.isfile(argument)
                ),
                None,
            )
            source_version = read_whisparr_version(source_binary)
            requested = normalize_whisparr_release_version(selector)
            preserve_saved_runtime = bool(
                source_version
                and (
                    source_version == requested
                    or (
                        selector in {"latest", "prerelease"}
                        and not config.get("auto_update")
                    )
                )
            )
        if (legacy_moving_install or preserve_saved_runtime) and not existing_binary:
            if self._adopt_legacy_whisparr(before_activate=before_activate):
                return True, None
        if existing_binary and not force:
            try:
                validate_whisparr_runtime(config, existing_binary)
            except WhisparrConfigurationError:
                pass
            else:
                current = read_whisparr_version(existing_binary)
                if legacy_moving_install:
                    os.chmod(existing_binary, 0o755)
                    return True, None
                requested = normalize_whisparr_release_version(selector)
                if requested and current == requested:
                    os.chmod(existing_binary, 0o755)
                    return True, None
                if selector in {"latest", "prerelease"} and not config.get(
                    "release_version_enabled"
                ):
                    return True, None
                tag = self._resolve_whisparr_release_tag()
                if current == normalize_whisparr_release_version(tag):
                    os.chmod(existing_binary, 0o755)
                    return True, None

        tag = self._resolve_whisparr_release_tag()
        owner, repo = whisparr_repository(config)
        transaction, candidate = self._prepare_whisparr_transaction()
        try:
            transaction.mark_building()
            success, error = Downloader().download_release_version(
                process_name="Whisparr",
                key="whisparr",
                repo_owner=owner,
                repo_name=repo,
                release_version=tag,
                target_dir=os.path.join(candidate, "Whisparr"),
            )
            if not success:
                return False, error
            candidate_binary = self._find_whisparr_binary(candidate)
            if not candidate_binary:
                raise WhisparrConfigurationError(
                    "Whisparr release did not contain its native Whisparr binary."
                )
            validate_whisparr_runtime(config, candidate_binary)
            installed = read_whisparr_version(candidate_binary)
            expected = normalize_whisparr_release_version(tag)
            if installed != expected:
                raise WhisparrConfigurationError(
                    f"Whisparr release metadata reports {installed!r}, "
                    f"but the selected release is {expected}."
                )
            os.chmod(candidate_binary, 0o755)
            os.chmod(candidate, 0o755)
            if before_activate is not None:
                before_activate()
            transaction.activate()
            transaction.commit()
            return True, None
        except Exception:
            if transaction.activated:
                transaction.rollback()
            raise
        finally:
            if not transaction.activated:
                transaction.abandon()

    @staticmethod
    def _format_bytes(value):
        try:
            size = float(value)
        except (TypeError, ValueError):
            return "unknown"
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        for unit in units:
            if abs(size) < 1024 or unit == units[-1]:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TiB"

    def _archive_storage_details(self, archive_path):
        try:
            archive_size = os.path.getsize(archive_path)
        except OSError:
            archive_size = None
        try:
            free_space = shutil.disk_usage(self.install_dir).free
        except OSError:
            free_space = None
        return archive_size, free_space

    def _log_archive_storage(self, archive_path):
        archive_size, free_space = self._archive_storage_details(archive_path)
        logger.info(
            "%s archive %s is %s (%s bytes); free space in %s is %s (%s bytes).",
            self.app_name_cap,
            os.path.basename(archive_path),
            self._format_bytes(archive_size),
            archive_size if archive_size is not None else "unknown",
            self.install_dir,
            self._format_bytes(free_space),
            free_space if free_space is not None else "unknown",
        )
        return archive_size, free_space

    def _run_tar(self, operation, archive_path, command):
        result = subprocess.run(
            command,
            check=False,
            cwd=self.install_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0:
            return

        archive_size, free_space = self._archive_storage_details(archive_path)
        stderr = (result.stderr or "").strip() or "tar produced no stderr output"
        runtime_hint = ""
        if "function not implemented" in stderr.lower():
            runtime_hint = (
                "GNU tar received ENOSYS (Function not implemented) while creating "
                "archive files. Ubuntu 26.04's security-hardened tar uses openat2 "
                "during extraction, which may be unavailable through an older host "
                "kernel, container runtime, or seccomp profile. "
            )
        raise RuntimeError(
            f"{self.app_name_cap} archive {operation} failed for "
            f"{os.path.basename(archive_path)} (tar exit {result.returncode}; "
            f"archive size {self._format_bytes(archive_size)} "
            f"[{archive_size if archive_size is not None else 'unknown'} bytes]; "
            f"free space {self._format_bytes(free_space)} "
            f"[{free_space if free_space is not None else 'unknown'} bytes]): "
            f"{runtime_hint}{stderr}"
        )

    def _validate_archive(self, archive_path):
        self._run_tar(
            "validation",
            archive_path,
            ["tar", "tzf", archive_path],
        )

    def _extract_archive(self, archive_path):
        try:
            self._run_tar(
                "extraction",
                archive_path,
                [
                    "tar",
                    "xzf",
                    archive_path,
                    "--no-same-owner",
                    "--no-same-permissions",
                ],
            )
        except RuntimeError as exc:
            if "function not implemented" not in str(exc).lower():
                raise
            logger.warning(
                "%s GNU tar extraction could not use openat2 through the host "
                "runtime. Retrying with Python's security-filtered extractor.",
                self.app_name_cap,
            )
            try:
                self._extract_archive_with_python(archive_path)
            except (OSError, tarfile.TarError, ValueError) as fallback_error:
                raise RuntimeError(
                    f"{exc} Security-filtered Python extraction also failed: "
                    f"{fallback_error}"
                ) from fallback_error

    def _extract_archive_with_python(self, archive_path):
        with tarfile.open(archive_path, mode="r:gz") as archive:
            archive.extractall(path=self.install_dir, filter="data")

    def _validate_and_extract_archive(self, archive_path):
        self._validate_archive(archive_path)
        self._log_archive_storage(archive_path)
        self._extract_archive(archive_path)

    def get_download_url(self):
        if self.whisparr_config is not None:
            tag = self._resolve_whisparr_release_tag()
            owner, repo = whisparr_repository(self.whisparr_config)
            downloader = Downloader()
            release, error = downloader.fetch_github_release_info(owner, repo, tag)
            if not release:
                raise WhisparrConfigurationError(error)
            arch = platform.machine().lower()
            architecture = {
                "x86_64": "x64",
                "amd64": "x64",
                "aarch64": "arm64",
                "arm64": "arm64",
                "armv7l": "arm",
            }.get(arch)
            if not architecture:
                raise WhisparrConfigurationError(f"Unsupported architecture: {arch}")
            runtime = (
                "linux-musl" if "musl" in platform.libc_ver()[0].lower() else "linux"
            )
            url, _ = downloader.find_asset_download_url(
                release, f"{runtime}-{architecture}"
            )
            if not url:
                raise WhisparrConfigurationError(
                    "No matching Whisparr Linux release asset."
                )
            return url
        arch = platform.machine()
        # Use configured branch if provided, otherwise use default branch based on app_name
        # Default branches: sonarr=main, radarr=master, prowlarr=master, readarr=develop, lidarr=master, whisparr=nightly, whisparr-v3=eros
        if not self.branch or self.branch == "main":
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

    def get_fallback_download_url(self):
        if self.app_name != "sonarr":
            return None
        arch = platform.machine()
        version_query = ""
        if self.version and self.version not in ("3", "4"):
            version_query = f"&version={self.version}"
        base_url = (
            f"https://{self.app_name}.servarr.com/v1/update/main/updatefile"
            f"?os=linux&runtime=netcore{version_query}"
        )
        if arch == "x86_64":
            return f"{base_url}&arch=x64"
        elif arch == "aarch64":
            return f"{base_url}&arch=arm64"
        elif arch == "armv7l":
            return f"{base_url}&arch=arm"
        return None

    def resolve_pinned_download_url(self):
        if self.whisparr_config is not None:
            try:
                return self.get_download_url(), None
            except WhisparrConfigurationError as error:
                return None, str(error)
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
        if self.whisparr_config is not None:
            return self.resolve_pinned_download_url()
        # Use config-provided repo_owner/repo_name if available
        if self.repo_owner and self.repo_name:
            owner, repo = self.repo_owner, self.repo_name
        else:
            # Fall back to hardcoded mappings
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

    def install(self, force=False, *, before_activate=None):
        try:
            if self.whisparr_config is not None:
                return self._install_whisparr(
                    force=force, before_activate=before_activate
                )
            logger.info(f"Installing {self.app_name_cap}...")

            os.makedirs(self.install_dir, exist_ok=True)
            binary_path = os.path.join(
                self.install_dir, self.app_name_cap, self.app_name_cap
            )
            if not force and os.path.exists(binary_path):
                if not os.access(binary_path, os.X_OK):
                    os.chmod(binary_path, 0o755)
                logger.info(
                    "%s binary already present at %s; skipping install.",
                    self.app_name_cap,
                    binary_path,
                )
                return True, None

            def download_with_wget(download_url):
                before_files = set(os.listdir(self.install_dir))
                subprocess.run(
                    ["wget", "--content-disposition", download_url],
                    check=True,
                    cwd=self.install_dir,
                )
                after_files = set(os.listdir(self.install_dir))
                return [name for name in (after_files - before_files) if name]

            def download_with_retries(download_urls, retries=3):
                last_error = None
                for attempt in range(1, retries + 1):
                    for download_url in download_urls:
                        if not download_url:
                            continue
                        try:
                            return download_with_wget(download_url), None
                        except subprocess.CalledProcessError as exc:
                            last_error = exc
                            logger.warning(
                                "%s download failed (attempt %s/%s): %s",
                                self.app_name_cap,
                                attempt,
                                retries,
                                exc,
                            )
                    if attempt < retries:
                        logger.info(
                            "%s download retry scheduled (attempt %s/%s).",
                            self.app_name_cap,
                            attempt + 1,
                            retries,
                        )
                        time.sleep(2**attempt)
                return None, last_error

            download_urls = [self.get_download_url(), self.get_fallback_download_url()]
            new_files, error = download_with_retries(download_urls)
            if not new_files:
                raise subprocess.CalledProcessError(
                    getattr(error, "returncode", 1),
                    getattr(error, "cmd", "wget"),
                )
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
                    for name in new_files:
                        try:
                            os.remove(os.path.join(self.install_dir, name))
                        except OSError:
                            continue
                    raise FileNotFoundError("Downloaded archive not found.")

            archive_path = os.path.join(self.install_dir, tarball)
            self._validate_and_extract_archive(archive_path)
            os.remove(archive_path)
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
        if self.whisparr_config is not None:
            try:
                tag = self._resolve_whisparr_release_tag()
                return normalize_whisparr_release_version(tag), None
            except (WhisparrConfigurationError, requests.RequestException) as error:
                return None, str(error)
        urls = [self.get_download_url()]
        fallback_url = self.get_fallback_download_url()
        if fallback_url and fallback_url not in urls:
            urls.append(fallback_url)

        errors = []
        for url in urls:
            for attempt in range(1, 3):
                response = None
                try:
                    response = requests.head(url, allow_redirects=True, timeout=10)
                    if response.status_code >= 400:
                        response = requests.get(
                            url, stream=True, allow_redirects=True, timeout=15
                        )
                    if response.status_code >= 400:
                        raise RuntimeError(f"HTTP {response.status_code} for {url}")
                    filename = None
                    content_disposition = response.headers.get(
                        "Content-Disposition", ""
                    )
                    match = re.search(r'filename="?([^"]+)"?', content_disposition)
                    if match:
                        filename = match.group(1)
                    if not filename:
                        filename = os.path.basename(response.url)
                    version = self.extract_version_from_filename(filename)
                    if version:
                        return version, None
                    errors.append(f"Unable to parse version from {filename}")
                except Exception as e:
                    errors.append(str(e))
                finally:
                    if response is not None:
                        try:
                            response.close()
                        except Exception:
                            pass
                if attempt < 2:
                    time.sleep(2**attempt)

        if errors:
            return None, " | ".join(errors)
        return None, "Failed to determine latest version."
