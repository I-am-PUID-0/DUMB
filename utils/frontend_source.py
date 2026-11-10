"""Validated image-built frontend identity; independent of runtime update policy."""

from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading

MANIFEST_NAME = ".dumb-build-source.json"
MAX_MANIFEST_BYTES = 16 * 1024
MAX_PACKAGE_BYTES = 1024 * 1024
MAX_ENTRIES = 50_000
MAX_RUNTIME_BYTES = 2 * 1024 * 1024 * 1024
MAX_DEPTH = 64
_PUBLIC_FIELDS = (
    "schema_version",
    "repository",
    "source_kind",
    "requested_ref",
    "commit_sha",
    "package_version",
)
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
)
_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()


class FrontendSourceError(ValueError):
    """The image source marker cannot establish the installed runtime's identity."""


@contextmanager
def _closing_fd(fd):
    try:
        yield fd
    finally:
        os.close(fd)


def _stat_key(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _open(root_fd, relative, flags):
    parent = os.dup(root_fd)
    try:
        parts = Path(relative).parts
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            os.close(parent)
            parent = child
        return os.open(parts[-1], flags | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)


def _read(root_fd, relative, limit):
    with _closing_fd(_open(root_fd, relative, os.O_RDONLY | os.O_NONBLOCK)) as fd:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise FrontendSourceError(
                "Frontend metadata must be a bounded regular file"
            )
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit or _stat_key(before) != _stat_key(os.fstat(fd)):
            raise FrontendSourceError("Frontend metadata changed while reading")
        return data, _stat_key(before)


def _identity(value):
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
        raise FrontendSourceError("Unsupported frontend source metadata")
    if value["schema_version"] != 1:
        raise FrontendSourceError("Unsupported frontend source metadata")
    identity = {field: value.get(field) for field in _PUBLIC_FIELDS}
    if (
        not isinstance(identity["repository"], str)
        or len(identity["repository"]) > 200
        or not _REPOSITORY.fullmatch(identity["repository"])
        or any(part in {".", ".."} for part in identity["repository"].split("/"))
        or not isinstance(identity["source_kind"], str)
        or identity["source_kind"] not in {"release", "branch", "commit"}
        or not isinstance(identity["requested_ref"], str)
        or not identity["requested_ref"]
        or len(identity["requested_ref"]) > 256
        or any(
            not character.isprintable() or character.isspace()
            for character in identity["requested_ref"]
        )
        or not isinstance(identity["commit_sha"], str)
        or not _COMMIT.fullmatch(identity["commit_sha"])
        or not isinstance(identity["package_version"], str)
        or len(identity["package_version"]) > 128
        or not _VERSION.fullmatch(identity["package_version"])
    ):
        raise FrontendSourceError("Invalid frontend source identity")
    if identity["source_kind"] == "commit":
        requested = identity["requested_ref"].lower()
        if not re.fullmatch(r"[0-9a-f]{7,40}", requested) or not identity[
            "commit_sha"
        ].startswith(requested):
            raise FrontendSourceError(
                "Frontend commit selector does not match its commit"
            )
    return identity


def _snapshot(root, root_fd):
    package, package_stat = _read(root_fd, "package.json", MAX_PACKAGE_BYTES)
    package_data = json.loads(package)
    if not isinstance(package_data, dict):
        raise FrontendSourceError("Frontend package metadata must be an object")
    package_version = package_data.get("version")
    if not isinstance(package_version, str) or not _VERSION.fullmatch(package_version):
        raise FrontendSourceError("Frontend package version is invalid")
    try:
        installed_marker, _ = _read(root_fd, "version.txt", MAX_MANIFEST_BYTES)
    except FileNotFoundError:
        installed_marker = b""
    if installed_marker.strip():
        raise FrontendSourceError("Runtime installer source marker takes precedence")

    entries = [("package.json", "file", package_stat, None)]
    total = 0

    def walk(fd, relative, depth):
        nonlocal total
        if depth > MAX_DEPTH:
            raise FrontendSourceError("Frontend runtime exceeds the supported depth")
        before = os.fstat(fd)
        with os.scandir(fd) as children:
            for child in children:
                if len(entries) >= MAX_ENTRIES:
                    raise FrontendSourceError(
                        "Frontend runtime exceeds the supported entry bound"
                    )
                name = relative / child.name
                value = child.stat(follow_symlinks=False)
                key = _stat_key(value)
                if stat.S_ISDIR(value.st_mode):
                    entries.append((name.as_posix(), "directory", key, None))
                    nested = os.open(
                        child.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fd,
                    )
                    try:
                        if key != _stat_key(os.fstat(nested)):
                            raise FrontendSourceError(
                                "Frontend directory changed while reading"
                            )
                        walk(nested, name, depth + 1)
                    finally:
                        os.close(nested)
                elif stat.S_ISREG(value.st_mode):
                    total += value.st_size
                    if total > MAX_RUNTIME_BYTES:
                        raise FrontendSourceError(
                            "Frontend runtime exceeds the supported size"
                        )
                    entries.append((name.as_posix(), "file", key, None))
                elif stat.S_ISLNK(value.st_mode):
                    target = os.readlink(child.name, dir_fd=fd)
                    output = root / ".output"
                    resolved = (root / name.parent / target).resolve(strict=True)
                    if Path(target).is_absolute() or not resolved.is_relative_to(
                        output
                    ):
                        raise FrontendSourceError(
                            "Frontend links must remain inside the runtime"
                        )
                    entries.append((name.as_posix(), "symlink", key, target))
                else:
                    raise FrontendSourceError(
                        "Frontend runtime contains a special file"
                    )
        if _stat_key(before) != _stat_key(os.fstat(fd)):
            raise FrontendSourceError("Frontend directory changed while reading")

    output_fd = _open(root_fd, ".output", os.O_RDONLY | os.O_DIRECTORY)
    try:
        entries.append((".output", "directory", _stat_key(os.fstat(output_fd)), None))
        walk(output_fd, Path(".output"), 0)
    finally:
        os.close(output_fd)
    entries.sort(key=lambda item: item[0])
    if not any(
        name == ".output/server/index.mjs" and kind == "file"
        for name, kind, _, _ in entries
    ):
        raise FrontendSourceError("Frontend runtime entry point is missing")
    return package_version, tuple(entries)


def _artifact_sha256(root, root_fd, entries):
    cache_key = str(root)
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached is not None and cached[0] == entries:
            _CACHE.move_to_end(cache_key)
            return cached[1]
    digest = hashlib.sha256()
    for name, kind, expected, target in entries:
        value = target
        if kind == "file":
            with _closing_fd(_open(root_fd, name, os.O_RDONLY | os.O_NONBLOCK)) as fd:
                if expected != _stat_key(os.fstat(fd)):
                    raise FrontendSourceError("Frontend file changed before hashing")
                contents = hashlib.sha256()
                while chunk := os.read(fd, 1024 * 1024):
                    contents.update(chunk)
                if expected != _stat_key(os.fstat(fd)):
                    raise FrontendSourceError("Frontend file changed while hashing")
                value = contents.hexdigest()
        digest.update(
            json.dumps(
                [name, kind, value], ensure_ascii=True, separators=(",", ":")
            ).encode("ascii")
            + b"\n"
        )
    result = digest.hexdigest()
    if entries != _snapshot(root, root_fd)[1]:
        raise FrontendSourceError("Frontend runtime changed while hashing")
    with _CACHE_LOCK:
        _CACHE[cache_key] = (entries, result)
        _CACHE.move_to_end(cache_key)
        while len(_CACHE) > 4:
            _CACHE.popitem(last=False)
    return result


def write_frontend_source(root, source_info):
    """Write provenance after a completed build; infer the actual package version."""
    if not isinstance(source_info, dict):
        raise FrontendSourceError("Frontend source identity must be an object")
    root = Path(root).resolve(strict=True)
    with _closing_fd(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)) as fd:
        package_version, entries = _snapshot(root, fd)
        identity = _identity(
            {
                **source_info,
                "schema_version": 1,
                "package_version": package_version,
            }
        )
        supplied_version = source_info.get("package_version")
        if supplied_version is not None and supplied_version != package_version:
            raise FrontendSourceError(
                "Source package version does not match the installed package"
            )
        manifest = {
            **identity,
            "artifact_sha256": _artifact_sha256(root, fd, entries),
        }
    target = root / MANIFEST_NAME
    if target.is_symlink():
        raise FrontendSourceError("Frontend source manifest cannot be a symbolic link")
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=".dumb-build-source-", dir=root
    )
    try:
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o644)
            json.dump(
                manifest,
                handle,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return manifest


def read_installed_frontend_source(root="/dumb/frontend"):
    """Return public identity only while its package and complete runtime still match."""
    try:
        root = Path(root).resolve(strict=True)
        with _closing_fd(
            os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        ) as fd:
            raw, _ = _read(fd, MANIFEST_NAME, MAX_MANIFEST_BYTES)
            manifest = json.loads(raw)
            identity = _identity(manifest)
            expected = manifest.get("artifact_sha256")
            if not isinstance(expected, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected
            ):
                return None
            package_version, entries = _snapshot(root, fd)
            if identity["package_version"] != package_version:
                return None
            if _artifact_sha256(root, fd, entries) != expected:
                return None
            return identity
    except (OSError, ValueError, RuntimeError):
        return None
