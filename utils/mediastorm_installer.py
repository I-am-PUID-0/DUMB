import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from utils.global_logger import logger
from utils.oci_image import OCIImageError, OCIRegistryClient

MEDIASTORM_PYTHON_PACKAGES = ("parsett==1.8.5", "subliminal==2.6.0")
MEDIASTORM_OCI_REGISTRY = "registry-1.docker.io"
MEDIASTORM_OCI_REPOSITORY = "godver3/mediastorm"
MEDIASTORM_OCI_REFERENCE = "latest"
_MEDIASTORM_RELEASE_PATTERN = re.compile(
    r"^v?(\d+\.\d+\.\d+)(?:-(\d{8}))?$", re.IGNORECASE
)
_MEDIASTORM_LEGACY_RELEASE_PATTERN = re.compile(
    r"^(\d+\.\d+\.\d+)(\d{8})$", re.IGNORECASE
)
_MEDIASTORM_COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$", re.IGNORECASE)
_MEDIASTORM_DIGEST_PATTERN = re.compile(r"^sha256:[a-f0-9]{64}$")
_MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
_SOURCE_FILES = {
    "app/mediastorm": "mediastorm",
    "root/mediastorm": "mediastorm",
    "app/version.txt": "app-version.txt",
    "parse_title.py": "scripts/parse_title.py",
    "parse_title_batch.py": "scripts/parse_title_batch.py",
    "search_subtitles.py": "scripts/search_subtitles.py",
    "download_subtitle.py": "scripts/download_subtitle.py",
    "detect_credits.py": "scripts/detect_credits.py",
    "usr/local/bin/ffmpeg": "bin/ffmpeg",
    "usr/local/bin/ffprobe": "bin/ffprobe",
    "usr/local/bin/yt-dlp": "bin/yt-dlp",
    "usr/local/bin/deno": "bin/deno",
}
_MEDIASTORM_COMPATIBILITY_LINK = ("root/mediastorm", "/app/mediastorm")
_SOURCE_DIRECTORIES = {
    "opt/strmr-web": "web",
    "opt/iroh": "iroh",
}


class MediaStormInstallError(RuntimeError):
    pass


def _normalize_layer_path(value: str) -> str:
    normalized = str(value or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise MediaStormInstallError("OCI layer contains an unsafe path.")
    return str(path)


def _mapped_path(source_path: str) -> str | None:
    if source_path in _SOURCE_FILES:
        return _SOURCE_FILES[source_path]
    for source_dir, target_dir in _SOURCE_DIRECTORIES.items():
        if source_path == source_dir:
            return target_dir
        prefix = f"{source_dir}/"
        if source_path.startswith(prefix):
            return f"{target_dir}/{source_path[len(prefix):]}"
    return None


def _safe_destination(root: Path, relative_path: str) -> Path:
    destination = root.joinpath(*PurePosixPath(relative_path).parts)
    root_resolved = root.resolve()
    try:
        destination.parent.resolve().relative_to(root_resolved)
    except ValueError as exc:
        raise MediaStormInstallError(
            "OCI layer escaped the staging directory."
        ) from exc
    return destination


def _remove_existing(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _apply_whiteout(root: Path, source_path: str) -> bool:
    source = PurePosixPath(source_path)
    name = source.name
    if name == ".wh..wh..opq":
        mapped_parent = _mapped_path(str(source.parent))
        if mapped_parent is None:
            return False
        destination = _safe_destination(root, mapped_parent)
        if destination.is_dir():
            for child in destination.iterdir():
                _remove_existing(child)
        return True
    if not name.startswith(".wh."):
        return False
    hidden_source = str(source.parent / name[4:])
    mapped = _mapped_path(hidden_source)
    if mapped is None:
        return False
    _remove_existing(_safe_destination(root, mapped))
    return True


def apply_mediastorm_layer(
    layer_path: str | Path,
    staging_root: str | Path,
    extracted_bytes: int = 0,
) -> int:
    root = Path(staging_root)
    root.mkdir(parents=True, exist_ok=True)
    try:
        archive = tarfile.open(layer_path, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise MediaStormInstallError(
            "OCI image layer is not a valid gzip tar."
        ) from exc
    with archive:
        for member in archive:
            source_path = _normalize_layer_path(member.name)
            if _apply_whiteout(root, source_path):
                continue
            mapped = _mapped_path(source_path)
            if mapped is None:
                continue
            destination = _safe_destination(root, mapped)
            if member.issym() or member.islnk():
                if (
                    member.issym()
                    and (source_path, member.linkname) == _MEDIASTORM_COMPATIBILITY_LINK
                ):
                    # Current upstream images keep /root/mediastorm as a
                    # compatibility alias while installing the real binary at
                    # /app/mediastorm. Extract the allowlisted regular file
                    # from its own layer and do not reproduce the absolute
                    # container symlink inside DUMB's staged runtime.
                    continue
                raise MediaStormInstallError(
                    f"MediaStorm OCI runtime contains an unsupported link: {source_path}"
                )
            if member.isdir():
                if destination.exists() and not destination.is_dir():
                    _remove_existing(destination)
                destination.mkdir(parents=True, exist_ok=True)
                destination.chmod(member.mode & 0o777)
                continue
            if not member.isfile():
                raise MediaStormInstallError(
                    f"MediaStorm OCI runtime contains an unsupported entry: {source_path}"
                )
            extracted_bytes += int(member.size or 0)
            if extracted_bytes > _MAX_EXTRACTED_BYTES:
                raise MediaStormInstallError(
                    "MediaStorm OCI runtime exceeds the size limit."
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                _remove_existing(destination)
            source = archive.extractfile(member)
            if source is None:
                raise MediaStormInstallError(
                    f"Unable to read MediaStorm OCI entry: {source_path}"
                )
            with source, destination.open("wb") as handle:
                shutil.copyfileobj(source, handle, length=1024 * 1024)
            destination.chmod(member.mode & 0o777)
    return extracted_bytes


def normalize_mediastorm_version(raw_value: str) -> str:
    parts = [line.strip() for line in str(raw_value or "").splitlines() if line.strip()]
    if not parts:
        return ""
    value = "-".join(parts)
    return value if value.startswith("v") else f"v{value}"


def mediastorm_app_version_text(raw_value: str) -> str:
    """Convert DUMB's normalized marker back to MediaStorm's file format."""
    normalized = normalize_mediastorm_version(raw_value)
    value = normalized.removeprefix("v")
    release_match = _MEDIASTORM_RELEASE_PATTERN.fullmatch(value)
    if not release_match:
        return f"{value}\n"
    version, build_id = release_match.groups()
    lines = [version]
    if build_id:
        lines.append(build_id)
    return "\n".join(lines) + "\n"


def mediastorm_install_selector(config: dict) -> str:
    if not config.get("release_version_enabled"):
        return MEDIASTORM_OCI_REFERENCE
    selector = str(config.get("release_version") or "").strip()
    if not selector:
        raise MediaStormInstallError(
            "MediaStorm release pinning is enabled but release_version is empty."
        )
    if selector.lower() == "latest":
        return MEDIASTORM_OCI_REFERENCE
    return selector


def _mediastorm_install_request(config: dict, requested_version: str) -> dict:
    selector = mediastorm_install_selector(config)
    if selector == MEDIASTORM_OCI_REFERENCE:
        expected_version = normalize_mediastorm_version(requested_version)
        if not expected_version or expected_version == "vlatest":
            raise MediaStormInstallError(
                "A concrete MediaStorm release is required for latest."
            )
        return {
            "selector": selector,
            "references": [MEDIASTORM_OCI_REFERENCE],
            "expected_version": expected_version,
            "expected_prefix": None,
        }

    digest = selector.lower()
    if _MEDIASTORM_DIGEST_PATTERN.fullmatch(digest):
        return {
            "selector": digest,
            "references": [digest],
            "expected_version": None,
            "expected_prefix": None,
        }

    commit = selector.lower()
    if _MEDIASTORM_COMMIT_PATTERN.fullmatch(commit):
        return {
            "selector": commit,
            "references": [commit],
            "expected_version": None,
            "expected_prefix": None,
        }

    legacy = _MEDIASTORM_LEGACY_RELEASE_PATTERN.fullmatch(selector)
    if legacy:
        version, release_date = legacy.groups()
        return {
            "selector": selector,
            "references": [selector],
            "expected_version": f"v{version}-{release_date}",
            "expected_prefix": None,
        }

    release = _MEDIASTORM_RELEASE_PATTERN.fullmatch(selector)
    if release:
        version, release_date = release.groups()
        if release_date:
            references = [f"{version}{release_date}", version]
            expected_version = f"v{version}-{release_date}"
            expected_prefix = None
        else:
            references = [version]
            expected_version = None
            expected_prefix = f"v{version}"
        return {
            "selector": selector,
            "references": references,
            "expected_version": expected_version,
            "expected_prefix": expected_prefix,
        }

    raise MediaStormInstallError(
        "Invalid MediaStorm release_version. Use latest, a release tag "
        "(for example 1.5.0 or v1.5.0-20260711), a full 40-character "
        "commit SHA, or a sha256 OCI digest."
    )


def mediastorm_runtime_matches_selection(
    runtime_dir: str | Path, selector: str
) -> bool:
    runtime = Path(runtime_dir)
    marker = runtime / "install-selector.txt"
    if not marker.is_file():
        # Runtimes installed before version selection support always used latest.
        return selector == MEDIASTORM_OCI_REFERENCE and mediastorm_runtime_ready(
            runtime
        )
    try:
        installed_selector = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return installed_selector.lower() == str(selector or "").strip().lower()


def mediastorm_runtime_ready(runtime_dir: str | Path) -> bool:
    runtime = Path(runtime_dir)
    required = (
        runtime / "mediastorm",
        runtime / "web" / "index.html",
        runtime / "iroh" / "iroh-direct-spike",
        runtime / "python-venv" / "bin" / "python3",
        runtime / "bin" / "ffmpeg",
        runtime / "bin" / "ffprobe",
        runtime / "bin" / "yt-dlp",
        runtime / "bin" / "deno",
    )
    return all(path.is_file() for path in required)


def _build_python_environment(runtime_dir: Path) -> None:
    python_executable = shutil.which("python3.11")
    if not python_executable:
        raise MediaStormInstallError("Python 3.11 is required to install MediaStorm.")
    commands = (
        [python_executable, "-m", "venv", str(runtime_dir / "python-venv")],
        [
            str(runtime_dir / "python-venv" / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            *MEDIASTORM_PYTHON_PACKAGES,
        ],
        [
            str(runtime_dir / "python-venv" / "bin" / "python"),
            "-m",
            "pip",
            "check",
        ],
    )
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            tail = detail[-1] if detail else "unknown error"
            raise MediaStormInstallError(
                f"Failed to prepare MediaStorm Python environment: {tail}"
            )


def _atomic_replace_runtime(staged_runtime: Path, runtime_dir: Path) -> None:
    backup = runtime_dir.parent / ".runtime-backup"
    if backup.exists() or backup.is_symlink():
        _remove_existing(backup)
    had_runtime = runtime_dir.exists() or runtime_dir.is_symlink()
    if had_runtime:
        os.replace(runtime_dir, backup)
    try:
        os.replace(staged_runtime, runtime_dir)
    except Exception:
        if had_runtime and backup.exists() and not runtime_dir.exists():
            os.replace(backup, runtime_dir)
        raise
    if backup.exists() or backup.is_symlink():
        _remove_existing(backup)


def install_mediastorm_runtime(
    config: dict,
    requested_version: str,
    *,
    client: OCIRegistryClient | None = None,
) -> dict:
    config_dir = Path(config.get("config_dir") or "/mediastorm")
    runtime_dir = config_dir / "runtime"
    repository = MEDIASTORM_OCI_REPOSITORY
    install_request = _mediastorm_install_request(config, requested_version)
    config_dir.mkdir(parents=True, exist_ok=True)
    registry_client = client or OCIRegistryClient(registry=MEDIASTORM_OCI_REGISTRY)

    manifest = None
    resolve_errors = []
    for reference in install_request["references"]:
        try:
            manifest = registry_client.resolve_manifest(repository, reference)
            break
        except OCIImageError as exc:
            resolve_errors.append(f"{reference}: {exc}")
    if manifest is None:
        raise MediaStormInstallError(
            "Unable to resolve the requested MediaStorm OCI reference: "
            + "; ".join(resolve_errors)
        )

    with tempfile.TemporaryDirectory(
        prefix=".mediastorm-install-", dir=config_dir
    ) as temp_dir:
        temp_root = Path(temp_dir)
        staged_runtime = temp_root / "runtime"
        staged_runtime.mkdir()
        extracted_bytes = 0
        layers = manifest["layers"]
        for index, descriptor in enumerate(layers, start=1):
            size_mb = int(descriptor.get("size", 0) or 0) / (1024 * 1024)
            logger.info(
                "Downloading MediaStorm OCI layer %d/%d (%.1f MiB).",
                index,
                len(layers),
                size_mb,
            )
            layer_path = temp_root / f"layer-{index}.tar.gz"
            try:
                registry_client.download_blob(repository, descriptor, layer_path)
                extracted_bytes = apply_mediastorm_layer(
                    layer_path, staged_runtime, extracted_bytes
                )
            except OCIImageError as exc:
                raise MediaStormInstallError(str(exc)) from exc
            finally:
                layer_path.unlink(missing_ok=True)

        upstream_version_path = staged_runtime / "app-version.txt"
        try:
            actual_version = normalize_mediastorm_version(
                upstream_version_path.read_text(encoding="utf-8")
            )
        except OSError as exc:
            raise MediaStormInstallError(
                "MediaStorm OCI image contains no version marker."
            ) from exc
        expected_version = install_request["expected_version"]
        expected_prefix = install_request["expected_prefix"]
        version_mismatch = expected_version and actual_version != expected_version
        prefix_mismatch = expected_prefix and not (
            actual_version == expected_prefix
            or actual_version.startswith(f"{expected_prefix}-")
        )
        if version_mismatch or prefix_mismatch:
            expectation = expected_version or f"{expected_prefix} release"
            raise MediaStormInstallError(
                "MediaStorm OCI version mismatch: "
                f"expected {expectation}, found {actual_version or 'unknown'}."
            )
        _build_python_environment(staged_runtime)
        (staged_runtime / "version.txt").write_text(
            f"{actual_version}\n", encoding="utf-8"
        )
        (staged_runtime / "image-digest.txt").write_text(
            f"{manifest['index_digest']}\n", encoding="utf-8"
        )
        (staged_runtime / "oci-reference.txt").write_text(
            f"{manifest['reference']}\n", encoding="utf-8"
        )
        (staged_runtime / "install-selector.txt").write_text(
            f"{install_request['selector']}\n", encoding="utf-8"
        )
        for executable in (
            staged_runtime / "mediastorm",
            staged_runtime / "iroh" / "iroh-direct-spike",
            staged_runtime / "bin" / "ffmpeg",
            staged_runtime / "bin" / "ffprobe",
            staged_runtime / "bin" / "yt-dlp",
            staged_runtime / "bin" / "deno",
        ):
            if executable.exists():
                executable.chmod(executable.stat().st_mode | 0o111)
        for script in (staged_runtime / "scripts").glob("*.py"):
            script.chmod(0o644)
        if not mediastorm_runtime_ready(staged_runtime):
            raise MediaStormInstallError(
                "Downloaded MediaStorm OCI runtime is incomplete."
            )
        _atomic_replace_runtime(staged_runtime, runtime_dir)

    return {
        "version": actual_version,
        "image_digest": manifest["index_digest"],
        "oci_reference": manifest["reference"],
        "install_selector": install_request["selector"],
        "runtime_dir": str(runtime_dir),
    }
