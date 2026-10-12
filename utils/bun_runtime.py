"""Provision Bun only from a publisher-authenticated, staged release artifact."""

import hashlib
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path

import requests

from utils.artifact_verification import (
    ArtifactVerificationError,
    verify_publisher_signature,
)
from utils.download import Downloader
from utils.global_logger import logger

BUN_RUNTIME_VERSION = "1.4.3"
_BUN_RELEASE_URL = (
    f"https://github.com/oven-sh/bun/releases/download/bun-v{BUN_RUNTIME_VERSION}"
)
_BUN_RUNTIME_LOCK = threading.Lock()
_BUN_METADATA_LIMIT = 1024 * 1024
_BUN_ARCHIVE_LIMIT = 256 * 1024 * 1024
_BUN_BINARY_LIMIT = 512 * 1024 * 1024


def _bun_version(binary, env):
    """Return a released version; missing, broken, and prerelease runtimes need repair."""
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:\+[\w.-]+)?", result.stdout.strip())
    return tuple(map(int, match.groups())) if match else None


def _bun_archive_name():
    """Use upstream's Linux image targets, including its legacy x64 baseline alias."""
    if platform.system() != "Linux":
        raise ArtifactVerificationError("Managed Bun provisioning requires Linux.")
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        target = "x64"
    elif machine in {"aarch64", "arm64"}:
        target = "aarch64"
    else:
        raise ArtifactVerificationError(f"Unsupported Bun architecture: {machine}.")
    # Match the upstream installer on Alpine; DUMB images use glibc. The baseline
    # alias also works on older x64 CPUs and is retained by current Bun releases.
    if Path("/etc/alpine-release").is_file():
        target += "-musl"
    if machine in {"x86_64", "amd64"}:
        target += "-baseline"
    return f"bun-linux-{target}.zip"


def _download_bun_file(name, destination, max_bytes):
    """Download a fixed official release URL with existing GitHub auth defaults."""
    url = f"{_BUN_RELEASE_URL}/{name}"
    # Requests strips Authorization on redirects to the GitHub asset CDN. Do not
    # reuse these headers for configurable URLs or disable TLS verification.
    with requests.get(
        url,
        headers=Downloader().get_headers(),
        stream=True,
        timeout=(15, 300),
    ) as response:
        response.raise_for_status()
        total = 0
        with destination.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ArtifactVerificationError(
                        f"Bun download exceeds the size limit: {name}."
                    )
                output.write(chunk)


def _verify_bun_archive(archive, manifest):
    """Require one exact archive entry in the already authenticated checksum list."""
    matches = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64}) (?: |\*)(.+)", line)
        if match and match.group(2) == archive.name:
            matches.append(match.group(1).lower())
    if len(matches) != 1:
        raise ArtifactVerificationError(
            f"Signed Bun manifest must contain one checksum for {archive.name}."
        )
    with archive.open("rb") as downloaded:
        digest = hashlib.file_digest(downloaded, "sha256").hexdigest()
    if digest != matches[0]:
        raise ArtifactVerificationError(f"Bun checksum mismatch: {archive.name}.")


def _extract_bun_binary(archive, candidate):
    """Copy only the exact regular Bun member, never extract archive-supplied paths."""
    root = archive.stem
    expected = f"{root}/bun"
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        executable = []
        for member in members:
            mode = member.external_attr >> 16
            if member.filename == f"{root}/" and member.is_dir():
                continue
            if (
                member.filename != expected
                or member.is_dir()
                or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                or member.file_size <= 0
                or member.file_size > _BUN_BINARY_LIMIT
                or member.flag_bits & 1
            ):
                raise ArtifactVerificationError(
                    f"Unexpected member in Bun archive: {member.filename!r}."
                )
            executable.append(member)
        if len(executable) != 1:
            raise ArtifactVerificationError("Bun archive must contain one executable.")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        with bundle.open(executable[0]) as source, candidate.open("xb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
        candidate.chmod(0o755)


def _trusted_bun_temp_root():
    """Use a root-owned sticky temp root, ignoring service-controlled TMPDIR."""
    root = Path("/tmp")
    info = root.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)
    ):
        raise ArtifactVerificationError("Bun verification requires a trusted /tmp.")
    return root


def _activate_bun_binary(candidate, binary, user_id, group_id):
    """Copy through held descriptors and atomically activate on the target filesystem."""
    binary.parent.mkdir(parents=True, exist_ok=True)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    destination_fd = os.open(binary.parent, directory_flags)
    stage_fd = None
    stage_info = None
    stage_trusted = False
    stage_name = f".dumb-bun-activate-{secrets.token_hex(12)}"
    try:
        os.mkdir(stage_name, mode=0o700, dir_fd=destination_fd)
        stage_fd = os.open(stage_name, directory_flags, dir_fd=destination_fd)
        stage_info = os.fstat(stage_fd)
        if (
            stage_info.st_uid != os.geteuid()
            or stat.S_IMODE(stage_info.st_mode) != 0o700
        ):
            raise ArtifactVerificationError("Unsafe Bun activation staging directory.")
        stage_trusted = True
        candidate_fd = os.open(
            "bun",
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode=0o700,
            dir_fd=stage_fd,
        )
        with os.fdopen(candidate_fd, "w+b") as output, candidate.open("rb") as source:
            expected = hashlib.file_digest(source, "sha256").hexdigest()
            source.seek(0)
            shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            output.seek(0)
            if hashlib.file_digest(output, "sha256").hexdigest() != expected:
                raise ArtifactVerificationError(
                    "Bun activation copy checksum mismatch."
                )
            os.fchmod(output.fileno(), 0o755)
            os.fchown(output.fileno(), user_id, group_id)
            # The private stage remains controller-owned. Its parent may be
            # renamed by the service, so neither side of this rename uses paths.
            os.replace("bun", "bun", src_dir_fd=stage_fd, dst_dir_fd=destination_fd)
    finally:
        try:
            if stage_trusted:
                try:
                    os.unlink("bun", dir_fd=stage_fd)
                except FileNotFoundError:
                    pass
                # Locate only our held inode if its service-owned parent renamed
                # the entry. Never recursively delete a substituted pathname.
                with os.scandir(destination_fd) as entries:
                    for entry in entries:
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if (info.st_dev, info.st_ino) == (
                                stage_info.st_dev,
                                stage_info.st_ino,
                            ):
                                os.rmdir(entry.name, dir_fd=destination_fd)
                                break
                        except FileNotFoundError:
                            continue
        except OSError as error:
            logger.warning("Could not remove private Bun activation stage: %s", error)
        finally:
            if stage_fd is not None:
                os.close(stage_fd)
            os.close(destination_fd)


def ensure_bun_runtime(process_handler, config_dir, env, user_id, group_id):
    """Authenticate managed upgrades, retaining newer custom versions and old binaries."""
    install_dir = Path(env["BUN_INSTALL"])
    binary = install_dir / "bin" / "bun"
    required = tuple(map(int, BUN_RUNTIME_VERSION.split(".")))
    with _BUN_RUNTIME_LOCK:
        current = _bun_version(binary, env) if binary.is_file() else None
        if current is not None and current >= required:
            return True, None
        logger.info(
            "Installing signed Bun %s in %s (existing version: %s).",
            BUN_RUNTIME_VERSION,
            install_dir,
            ".".join(map(str, current)) if current else "missing or unreadable",
        )
        try:
            archive_name = _bun_archive_name()
            install_dir.mkdir(parents=True, exist_ok=True)
            # Verify and probe outside service-owned ancestors. Activation copies
            # the authenticated bytes through held descriptors onto the target
            # filesystem, preserving atomic replacement across different mounts.
            with tempfile.TemporaryDirectory(
                prefix="dumb-bun-trust-", dir=_trusted_bun_temp_root()
            ) as staging:
                staged = Path(staging)
                manifest = staged / "SHASUMS256.txt"
                signature = staged / "SHASUMS256.txt.asc"
                archive = staged / archive_name
                _download_bun_file(manifest.name, manifest, _BUN_METADATA_LIMIT)
                _download_bun_file(signature.name, signature, _BUN_METADATA_LIMIT)
                verify_publisher_signature(manifest, signature, "bun")
                _download_bun_file(archive.name, archive, _BUN_ARCHIVE_LIMIT)
                _verify_bun_archive(archive, manifest)
                candidate = staged / "bin" / "bun"
                _extract_bun_binary(archive, candidate)
                # Candidate code is authenticated, so do not let service-provided
                # loader or preload variables execute unrelated code in its probe.
                install_env = {
                    name: value
                    for name, value in env.items()
                    if not name.startswith(("LD_", "DYLD_"))
                    and name not in {"GLIBC_TUNABLES", "BUN_OPTIONS", "NODE_OPTIONS"}
                }
                install_env["BUN_INSTALL"] = staging
                install_env["HOME"] = staging
                installed = _bun_version(candidate, install_env)
                if installed != required:
                    actual = (
                        ".".join(map(str, installed)) if installed else "unreadable"
                    )
                    raise ArtifactVerificationError(
                        f"Bun runtime validation failed: expected {BUN_RUNTIME_VERSION}, "
                        f"found {actual}."
                    )
                _activate_bun_binary(candidate, binary, user_id, group_id)
        except (
            ArtifactVerificationError,
            requests.RequestException,
            OSError,
            UnicodeError,
            zipfile.BadZipFile,
        ) as error:
            return (
                False,
                f"Error installing Bun {BUN_RUNTIME_VERSION}: {error} Existing runtime was preserved.",
            )
        return True, None
