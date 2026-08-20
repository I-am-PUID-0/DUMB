"""Verified AIOStreams runtime installation from the official OCI image."""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from utils.global_logger import logger
from utils.oci_image import OCIImageError, OCIRegistryClient

AIOSTREAMS_OCI_REGISTRY = "ghcr.io"
AIOSTREAMS_OCI_REPOSITORY = "viren070/aiostreams"
AIOSTREAMS_OCI_REFERENCE = "latest"

_AIOSTREAMS_RELEASE_PATTERN = re.compile(r"^v?(\d+\.\d+\.\d+)$")
_AIOSTREAMS_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
_MIMALLOC_PATH_PATTERN = re.compile(r"^usr/local/lib/libmimalloc\.so\.2(?:\.\d+)*$")
_MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024


class AIOStreamsInstallError(RuntimeError):
    pass


def aiostreams_install_selector(config: dict) -> str:
    if not config.get("release_version_enabled"):
        return AIOSTREAMS_OCI_REFERENCE
    selector = str(config.get("release_version") or "").strip()
    if not selector:
        raise AIOStreamsInstallError(
            "AIOStreams release pinning is enabled but release_version is empty."
        )
    if selector.lower() == AIOSTREAMS_OCI_REFERENCE:
        return AIOSTREAMS_OCI_REFERENCE
    if not _AIOSTREAMS_RELEASE_PATTERN.fullmatch(selector):
        raise AIOStreamsInstallError(
            "Invalid AIOStreams release_version. Use latest or a stable release "
            "tag such as v2.33.2."
        )
    return selector


def _manifest_references(selector: str) -> list[str]:
    if selector == AIOSTREAMS_OCI_REFERENCE:
        return [selector]
    version = selector.removeprefix("v")
    references = [selector, version]
    return list(dict.fromkeys(references))


def _resolve_manifest(client: OCIRegistryClient, selector: str) -> dict:
    errors = []
    for reference in _manifest_references(selector):
        try:
            return client.resolve_manifest(AIOSTREAMS_OCI_REPOSITORY, reference)
        except OCIImageError as exc:
            errors.append(f"{reference}: {exc}")
    raise AIOStreamsInstallError(
        "Unable to resolve the requested AIOStreams OCI reference: " + "; ".join(errors)
    )


def _normalize_layer_path(value: str) -> str:
    normalized = str(value or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise AIOStreamsInstallError("OCI layer contains an unsafe path.")
    return str(path)


def _mapped_path(source_path: str) -> str | None:
    if _MIMALLOC_PATH_PATTERN.fullmatch(source_path):
        return f"lib/{PurePosixPath(source_path).name}"
    if source_path == "app":
        return ""
    if source_path.startswith("app/"):
        return source_path[4:]
    return None


def _safe_destination(root: Path, relative_path: str) -> Path:
    if not relative_path:
        return root
    destination = root.joinpath(*PurePosixPath(relative_path).parts)
    root_resolved = root.resolve()
    try:
        destination.parent.resolve().relative_to(root_resolved)
    except ValueError as exc:
        raise AIOStreamsInstallError(
            "OCI layer escaped the AIOStreams staging directory."
        ) from exc
    return destination


def _remove_existing(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _mapped_link_target(source_path: str, linkname: str) -> tuple[str, str]:
    target = str(linkname or "").replace("\\", "/")
    if not target:
        raise AIOStreamsInstallError(
            f"AIOStreams OCI runtime contains an empty link: {source_path}"
        )
    if target.startswith("/"):
        resolved_source = posixpath.normpath(target[1:])
    else:
        resolved_source = posixpath.normpath(
            posixpath.join(posixpath.dirname(source_path), target)
        )
    if resolved_source == ".." or resolved_source.startswith("../"):
        raise AIOStreamsInstallError(
            f"AIOStreams OCI runtime link escapes /app: {source_path}"
        )
    mapped_target = _mapped_path(resolved_source)
    if mapped_target is None:
        raise AIOStreamsInstallError(
            f"AIOStreams OCI runtime link leaves the allowlisted runtime: {source_path}"
        )
    mapped_source = _mapped_path(source_path)
    if mapped_source is None:
        raise AIOStreamsInstallError(
            f"AIOStreams OCI runtime contains an unsupported link: {source_path}"
        )
    relative_target = posixpath.relpath(
        mapped_target or ".", posixpath.dirname(mapped_source) or "."
    )
    return mapped_target, relative_target


def _mapped_hardlink_target(source_path: str, linkname: str) -> str:
    target = str(linkname or "").replace("\\", "/").lstrip("/")
    try:
        normalized = _normalize_layer_path(target)
    except AIOStreamsInstallError as exc:
        raise AIOStreamsInstallError(
            f"AIOStreams OCI hard link is unsafe: {source_path}"
        ) from exc
    mapped_target = _mapped_path(normalized)
    if mapped_target is None:
        raise AIOStreamsInstallError(
            f"AIOStreams OCI hard link leaves the allowlisted runtime: {source_path}"
        )
    return mapped_target


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


def apply_aiostreams_layer(
    layer_path: str | Path,
    staging_root: str | Path,
    extracted_bytes: int = 0,
) -> int:
    """Apply only the image's /app tree to a bounded staging directory."""
    root = Path(staging_root)
    root.mkdir(parents=True, exist_ok=True)
    pending_hardlinks: list[tuple[Path, str, str]] = []
    try:
        archive = tarfile.open(layer_path, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise AIOStreamsInstallError(
            "AIOStreams OCI image layer is not a valid gzip tar."
        ) from exc

    with archive:
        for member in archive:
            source_path = _normalize_layer_path(member.name)
            if _apply_whiteout(root, source_path):
                continue
            mapped = _mapped_path(source_path)
            if mapped is None:
                continue
            if not mapped and not member.isdir():
                raise AIOStreamsInstallError("AIOStreams OCI /app root is invalid.")
            destination = _safe_destination(root, mapped)

            if member.isdir():
                if destination.exists() and not destination.is_dir():
                    _remove_existing(destination)
                destination.mkdir(parents=True, exist_ok=True)
                destination.chmod(member.mode & 0o777)
                continue

            if member.issym():
                _mapped_target, relative_target = _mapped_link_target(
                    source_path, member.linkname
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    _remove_existing(destination)
                destination.symlink_to(relative_target)
                continue

            if member.islnk():
                mapped_target = _mapped_hardlink_target(source_path, member.linkname)
                pending_hardlinks.append((destination, mapped_target, source_path))
                continue

            if not member.isfile():
                raise AIOStreamsInstallError(
                    f"AIOStreams OCI runtime contains an unsupported entry: {source_path}"
                )
            extracted_bytes += int(member.size or 0)
            if extracted_bytes > _MAX_EXTRACTED_BYTES:
                raise AIOStreamsInstallError(
                    "AIOStreams OCI runtime exceeds the extracted size limit."
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                _remove_existing(destination)
            source = archive.extractfile(member)
            if source is None:
                raise AIOStreamsInstallError(
                    f"Unable to read AIOStreams OCI entry: {source_path}"
                )
            with source, destination.open("wb") as handle:
                shutil.copyfileobj(source, handle, length=1024 * 1024)
            destination.chmod(member.mode & 0o777)

    unresolved = pending_hardlinks
    while unresolved:
        remaining = []
        progress = False
        for destination, mapped_target, source_path in unresolved:
            target = _safe_destination(root, mapped_target)
            if not target.is_file():
                remaining.append((destination, mapped_target, source_path))
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                _remove_existing(destination)
            os.link(target, destination)
            progress = True
        if not progress and remaining:
            raise AIOStreamsInstallError(
                f"AIOStreams OCI hard-link target is unavailable: {remaining[0][2]}"
            )
        unresolved = remaining
    return extracted_bytes


def _read_runtime_version(runtime_dir: str | Path) -> str:
    runtime = Path(runtime_dir)
    try:
        package = json.loads((runtime / "package.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return ""
    version = str(package.get("version") or "").strip()
    return version if _AIOSTREAMS_VERSION_PATTERN.fullmatch(version) else ""


def aiostreams_runtime_ready(runtime_dir: str | Path) -> bool:
    runtime = Path(runtime_dir)
    required = (
        runtime / "package.json",
        runtime / "LICENSE",
        runtime / "packages" / "server" / "package.json",
        runtime / "packages" / "server" / "dist" / "server.js",
        runtime / "packages" / "core" / "package.json",
        runtime / "packages" / "frontend" / "dist" / "index.html",
        runtime / "packages" / "core" / "dist" / "index.js",
        runtime / "resources" / "metadata.json",
        runtime / "lib" / "libmimalloc.so.2",
    )
    try:
        required_ready = all(
            path.is_file() and path.stat().st_size > 0 for path in required
        )
    except OSError:
        return False
    if not required_ready:
        return False
    if not _read_runtime_version(runtime):
        return False
    try:
        metadata = json.loads(
            (runtime / "resources" / "metadata.json").read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError):
        return False
    dependency_dirs = (
        runtime / "node_modules",
        runtime / "packages" / "server" / "node_modules",
        runtime / "packages" / "core" / "node_modules",
    )
    try:
        dependencies_ready = all(
            path.is_dir() and next(path.iterdir(), None) is not None
            for path in dependency_dirs
        )
    except OSError:
        return False
    return isinstance(metadata, dict) and dependencies_ready


def aiostreams_runtime_matches_selection(
    runtime_dir: str | Path, selector: str
) -> bool:
    runtime = Path(runtime_dir)
    marker = runtime / "install-selector.txt"
    try:
        installed_selector = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return installed_selector.casefold() == str(selector or "").strip().casefold()


def _validate_node_runtime(runtime: Path) -> None:
    node = shutil.which("node")
    if not node:
        raise AIOStreamsInstallError("Node.js 24 or newer is required by AIOStreams.")
    version_result = subprocess.run(
        [node, "--version"], capture_output=True, text=True, timeout=15
    )
    match = re.fullmatch(r"v(\d+)(?:\.\d+){2}", version_result.stdout.strip())
    if version_result.returncode != 0 or not match or int(match.group(1)) < 24:
        raise AIOStreamsInstallError("Node.js 24 or newer is required by AIOStreams.")
    server = runtime / "packages" / "server" / "dist" / "server.js"
    syntax_result = subprocess.run(
        [node, "--check", str(server)],
        cwd=runtime,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if syntax_result.returncode != 0:
        detail = (syntax_result.stderr or syntax_result.stdout or "").strip()
        raise AIOStreamsInstallError(
            "AIOStreams server runtime failed Node.js validation"
            + (f": {detail.splitlines()[-1]}" if detail else ".")
        )
    native_probe = (
        "const {createRequire}=require('module');"
        "const r=createRequire(process.argv[1] + '/packages/core/package.json');"
        "const Database=r('better-sqlite3');"
        "const db=new Database(':memory:');db.close();"
    )
    native_result = subprocess.run(
        [node, "-e", native_probe, str(runtime)],
        cwd=runtime,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if native_result.returncode != 0:
        detail = (native_result.stderr or native_result.stdout or "").strip()
        raise AIOStreamsInstallError(
            "AIOStreams native SQLite dependency failed validation"
            + (f": {detail.splitlines()[-1]}" if detail else ".")
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


def aiostreams_target_status(
    config: dict, *, client: OCIRegistryClient | None = None
) -> dict:
    selector = aiostreams_install_selector(config)
    runtime = Path(config.get("config_dir") or "/aiostreams") / "runtime"
    registry_client = client or OCIRegistryClient(registry=AIOSTREAMS_OCI_REGISTRY)
    manifest = _resolve_manifest(registry_client, selector)

    def read_marker(name: str) -> str:
        try:
            return (runtime / name).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    current_version = _read_runtime_version(runtime) or read_marker("version.txt")
    current_digest = read_marker("image-digest.txt").lower()
    available_digest = str(manifest["index_digest"]).strip().lower()
    installed = bool(
        aiostreams_runtime_ready(runtime)
        and aiostreams_runtime_matches_selection(runtime, selector)
        and current_digest
        and current_digest == available_digest
    )
    return {
        "selector": selector,
        "current_version": current_version,
        "current_digest": current_digest,
        "available_digest": available_digest,
        "installed": installed,
    }


def install_aiostreams_runtime(
    config: dict, *, client: OCIRegistryClient | None = None
) -> dict:
    config_dir = Path(config.get("config_dir") or "/aiostreams")
    runtime_dir = config_dir / "runtime"
    selector = aiostreams_install_selector(config)
    config_dir.mkdir(parents=True, exist_ok=True)
    registry_client = client or OCIRegistryClient(registry=AIOSTREAMS_OCI_REGISTRY)
    manifest = _resolve_manifest(registry_client, selector)

    with tempfile.TemporaryDirectory(
        prefix=".aiostreams-install-", dir=config_dir
    ) as temp_dir:
        temp_root = Path(temp_dir)
        staged_runtime = temp_root / "runtime"
        staged_runtime.mkdir()
        extracted_bytes = 0
        for index, descriptor in enumerate(manifest["layers"], start=1):
            size_mb = int(descriptor.get("size", 0) or 0) / (1024 * 1024)
            logger.info(
                "Downloading AIOStreams OCI layer %d/%d (%.1f MiB).",
                index,
                len(manifest["layers"]),
                size_mb,
            )
            layer_path = temp_root / f"layer-{index}.tar.gz"
            try:
                registry_client.download_blob(
                    AIOSTREAMS_OCI_REPOSITORY, descriptor, layer_path
                )
                extracted_bytes = apply_aiostreams_layer(
                    layer_path, staged_runtime, extracted_bytes
                )
            except OCIImageError as exc:
                raise AIOStreamsInstallError(str(exc)) from exc
            finally:
                layer_path.unlink(missing_ok=True)

        if not aiostreams_runtime_ready(staged_runtime):
            raise AIOStreamsInstallError(
                "Downloaded AIOStreams OCI runtime is incomplete."
            )
        actual_version = _read_runtime_version(staged_runtime)
        if selector != AIOSTREAMS_OCI_REFERENCE:
            expected_version = selector.removeprefix("v")
            if actual_version != expected_version:
                raise AIOStreamsInstallError(
                    "AIOStreams OCI version mismatch: "
                    f"expected {expected_version}, found {actual_version or 'unknown'}."
                )
        _validate_node_runtime(staged_runtime)
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
            f"{selector}\n", encoding="utf-8"
        )
        _atomic_replace_runtime(staged_runtime, runtime_dir)

    return {
        "version": actual_version,
        "image_digest": manifest["index_digest"],
        "oci_reference": manifest["reference"],
        "install_selector": selector,
        "runtime_dir": str(runtime_dir),
    }
