"""Verified persistent caches and install-operation telemetry.

The cache stores only reproducible inputs and build artifacts. Runtime data and
service configuration are never placed in it. Cache corruption is handled by
quarantining the affected entry and treating it as a miss.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from utils.global_logger import logger

DEFAULT_CACHE_ROOT = "/config/.cache/dumb"
CHUNK_SIZE = 1024 * 1024
MAX_RECENT_OPERATIONS = 50
LEGACY_BUCKET_PATTERN = re.compile(r"^.+-[0-9a-fA-F]{12}$")
INSTALL_CACHE_CLEANUP_SCOPES = frozenset(
    {"downloads", "dependencies", "artifacts", "quarantine", "legacy"}
)
_CACHE_ROOT_OVERRIDE: Path | None = None
_CACHE_ROOT_LOCK = threading.RLock()


def _cache_path_mode(path: Path, *, directory: bool) -> int:
    """Return a controller-writable mode without changing executable intent."""
    current = stat.S_IMODE(path.lstat().st_mode)
    if directory:
        return (current & 0o055) | 0o700
    return (current & 0o155) | 0o600


def _secure_cache_entry(path: Path, user_id: int, group_id: int) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        os.chown(path, user_id, group_id, follow_symlinks=False)
        return
    if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
        raise OSError(f"cache contains an unsupported file type: {path}")
    if info.st_uid != user_id or info.st_gid != group_id:
        os.chown(path, user_id, group_id, follow_symlinks=False)
    mode = _cache_path_mode(path, directory=stat.S_ISDIR(info.st_mode))
    if stat.S_IMODE(info.st_mode) != mode:
        os.chmod(path, mode, follow_symlinks=False)


def _validate_cache_tree(root: Path) -> None:
    """Refuse cache roots that could redirect ownership work elsewhere."""
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise OSError(f"refusing symlinked install-cache root: {root}")
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"install-cache root is not a directory: {root}")
    for current_root, directories, filenames in os.walk(root, followlinks=False):
        current = Path(current_root)
        for name in directories:
            candidate = current / name
            if candidate.is_symlink():
                continue
            if candidate.is_mount():
                raise OSError(
                    f"refusing nested mount inside install-cache root: {candidate}"
                )
        for name in filenames:
            candidate = current / name
            mode = candidate.lstat().st_mode
            if not stat.S_ISREG(mode) and not stat.S_ISLNK(mode):
                raise OSError(f"cache contains an unsupported file type: {candidate}")


def _repair_cache_tree(root: Path, user_id: int, group_id: int) -> None:
    """Restore controller ownership after a cache root or namespace is chowned."""
    info = root.lstat()
    owner_changed = info.st_uid != user_id or info.st_gid != group_id
    mode_changed = stat.S_IMODE(info.st_mode) != _cache_path_mode(root, directory=True)
    if not owner_changed and not mode_changed:
        return

    _validate_cache_tree(root)
    if owner_changed:
        for current_root, directories, filenames in os.walk(
            root, topdown=False, followlinks=False
        ):
            current = Path(current_root)
            for name in filenames:
                _secure_cache_entry(current / name, user_id, group_id)
            for name in directories:
                _secure_cache_entry(current / name, user_id, group_id)
    _secure_cache_entry(root, user_id, group_id)


def _isolated_fallback_cache_root() -> Path:
    root = Path(
        tempfile.mkdtemp(
            prefix=f"dumb-install-cache-{os.geteuid()}-",
            dir="/tmp",
        )
    )
    os.chmod(root, 0o700)
    return root


def _set_cache_root_override(path: Path) -> None:
    global _CACHE_ROOT_OVERRIDE
    with _CACHE_ROOT_LOCK:
        _CACHE_ROOT_OVERRIDE = path


def _install_cache_config() -> dict:
    try:
        from utils.config_loader import CONFIG_MANAGER

        dumb = CONFIG_MANAGER.get("dumb") or {}
        value = dumb.get("install_cache") or {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def install_cache_enabled() -> bool:
    return bool(_install_cache_config().get("enabled", True))


def install_cache_root() -> Path:
    with _CACHE_ROOT_LOCK:
        if _CACHE_ROOT_OVERRIDE is not None:
            return _CACHE_ROOT_OVERRIDE
    configured = str(_install_cache_config().get("path") or DEFAULT_CACHE_ROOT).strip()
    requested = Path(configured or DEFAULT_CACHE_ROOT)
    try:
        resolved = requested.parent.resolve() / requested.name
    except OSError:
        resolved = Path(DEFAULT_CACHE_ROOT)
    unsafe_roots = {
        Path("/"),
        Path("/config"),
        Path("/data"),
        Path("/home"),
        Path("/mnt"),
        Path("/root"),
        Path("/usr"),
        Path("/var"),
    }
    return Path(DEFAULT_CACHE_ROOT).resolve() if resolved in unsafe_roots else resolved


def shared_cache_path(namespace: str, *parts: str) -> str:
    """Return a namespaced shared cache path, creating it when possible."""
    safe_namespace = (
        "".join(
            character if character.isalnum() or character in "._-" else "_"
            for character in str(namespace or "shared")
        ).strip("._-")
        or "shared"
    )
    safe_parts = []
    for part in parts:
        safe_part = (
            "".join(
                character if character.isalnum() or character in "._-" else "_"
                for character in str(part or "default")
            ).strip("._-")
            or "default"
        )
        safe_parts.append(safe_part)
    cache = globals().get("INSTALL_CACHE")
    cache_root = install_cache_root()
    if cache is not None:
        cache.ensure()
        cache_root = cache.root
    path = cache_root / "dependencies" / safe_namespace
    for safe_part in safe_parts:
        path /= safe_part
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        fallback = Path("/tmp/dumb-install-cache") / "dependencies" / safe_namespace
        for safe_part in safe_parts:
            fallback /= safe_part
        fallback.mkdir(parents=True, exist_ok=True)
        return str(fallback)
    return str(path)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _directory_size(path: Path) -> tuple[int, int]:
    total = 0
    files = 0
    if not path.exists():
        return total, files
    for root, directories, filenames in os.walk(path, followlinks=False):
        directories[:] = [
            name for name in directories if not Path(root, name).is_symlink()
        ]
        for filename in filenames:
            candidate = Path(root, filename)
            try:
                if candidate.is_symlink():
                    continue
                total += candidate.stat().st_size
                files += 1
            except OSError:
                continue
    return total, files


class InstallCache:
    _lock = threading.RLock()

    def __init__(
        self,
        root: str | Path | None = None,
        legacy_config_root: str | Path = "/config",
    ):
        self._uses_configured_root = root is None
        self.requested_root = Path(root) if root is not None else install_cache_root()
        self.fallback_reason: str | None = None
        self.legacy_config_root = Path(legacy_config_root)
        self._set_root(self.requested_root)

    def _set_root(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects = self.root / "downloads" / "objects"
        self.index = self.root / "downloads" / "index"
        self.artifacts = self.root / "artifacts"
        self.quarantine = self.root / "quarantine"
        self.telemetry_db = self.root / "install-operations.sqlite"

    def ensure(self) -> None:
        with self._lock:
            try:
                self._ensure_at_root(self.root)
            except (OSError, sqlite3.Error) as error:
                if self.fallback_reason is not None:
                    raise
                self.fallback_reason = str(error)
                fallback = _isolated_fallback_cache_root()
                logger.error(
                    "Install cache %s is unsafe or unavailable (%s); using isolated "
                    "temporary cache %s for this DUMB process.",
                    self.root,
                    error,
                    fallback,
                )
                self._set_root(fallback)
                if self._uses_configured_root:
                    _set_cache_root_override(fallback)
                self._ensure_at_root(self.root)

    def _ensure_at_root(self, root: Path) -> None:
        if root.is_symlink():
            raise OSError(f"refusing symlinked install-cache root: {root}")
        root.mkdir(parents=True, exist_ok=True)
        controller_uid = os.geteuid()
        controller_gid = os.getegid()
        _repair_cache_tree(root, controller_uid, controller_gid)
        for path in (
            self.objects,
            self.index,
            self.artifacts,
            self.quarantine,
            self.root / "dependencies",
            self.root / "transactions",
        ):
            if path.is_symlink():
                raise OSError(f"refusing symlinked install-cache namespace: {path}")
            path.mkdir(parents=True, exist_ok=True)
            if path.is_mount():
                raise OSError(f"refusing mounted install-cache namespace: {path}")
            _repair_cache_tree(path, controller_uid, controller_gid)
        self._initialize_database()
        for path in (
            self.telemetry_db,
            self.telemetry_db.with_name(f"{self.telemetry_db.name}-wal"),
            self.telemetry_db.with_name(f"{self.telemetry_db.name}-shm"),
        ):
            if path.exists() or path.is_symlink():
                _secure_cache_entry(path, controller_uid, controller_gid)

    def _initialize_database(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.telemetry_db, timeout=5) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS install_operations (
                    operation_id TEXT PRIMARY KEY,
                    process_name TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    cache_hits INTEGER NOT NULL DEFAULT 0,
                    cache_misses INTEGER NOT NULL DEFAULT 0,
                    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                    rollback_performed INTEGER NOT NULL DEFAULT 0,
                    message TEXT
                )
                """)

    def _object_path(self, digest: str) -> Path:
        return self.objects / digest[:2] / digest

    def _index_path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()
        return self.index / f"{key}.json"

    def lookup_content(self, digest: str) -> Path | None:
        """Return a verified content-addressed object, or quarantine it."""
        normalized = str(digest or "").lower().removeprefix("sha256:")
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            return None
        self.ensure()
        object_path = self._object_path(normalized)
        try:
            if not object_path.is_file() or object_path.is_symlink():
                return None
            if sha256_file(object_path) != normalized:
                self.quarantine_path(object_path, "digest-mismatch")
                return None
            os.utime(object_path, None)
            return object_path
        except OSError:
            return None

    def store_content_file(self, source_path: str | Path, digest: str) -> Path:
        """Atomically retain an already-downloaded file by verified digest."""
        normalized = str(digest or "").lower().removeprefix("sha256:")
        source = Path(source_path)
        if len(normalized) != 64 or sha256_file(source) != normalized:
            raise OSError("content cache source failed digest verification")
        self.ensure()
        destination = self._object_path(normalized)
        with self._lock:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                temporary = destination.with_name(
                    f".{destination.name}.{uuid.uuid4().hex}.part"
                )
                shutil.copy2(source, temporary)
                if sha256_file(temporary) != normalized:
                    temporary.unlink(missing_ok=True)
                    raise OSError("content cache copy failed digest verification")
                os.replace(temporary, destination)
        return destination

    def lookup_download(self, url: str) -> tuple[bytes | None, dict]:
        if not install_cache_enabled():
            return None, {}
        self.ensure()
        index_path = self._index_path(url)
        try:
            metadata = json.loads(index_path.read_text(encoding="utf-8"))
            digest = str(metadata.get("sha256") or "")
            if len(digest) != 64:
                raise ValueError("invalid cached digest")
            object_path = self.lookup_content(digest)
            if object_path is None:
                raise ValueError("cached object is missing")
            return object_path.read_bytes(), metadata
        except (OSError, ValueError, json.JSONDecodeError) as error:
            if index_path.exists():
                logger.warning("Ignoring invalid download cache entry: %s", error)
                self.quarantine_path(index_path, "invalid-index")
            return None, {}

    def store_download(
        self,
        url: str,
        content: bytes,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> dict:
        if not install_cache_enabled():
            return {}
        self.ensure()
        digest = hashlib.sha256(content).hexdigest()
        object_path = self._object_path(digest)
        metadata = {
            "url": url,
            "sha256": digest,
            "size": len(content),
            "etag": etag or "",
            "last_modified": last_modified or "",
            "filename": filename or "",
            "content_type": content_type or "",
            "stored_at": int(time.time()),
        }
        with self._lock:
            object_path.parent.mkdir(parents=True, exist_ok=True)
            if not object_path.exists():
                temporary = object_path.with_name(
                    f".{object_path.name}.{uuid.uuid4().hex}.part"
                )
                with open(temporary, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                if sha256_file(temporary) != digest:
                    temporary.unlink(missing_ok=True)
                    raise OSError("download cache write failed integrity verification")
                os.replace(temporary, object_path)
            _atomic_json(self._index_path(url), metadata)
        return metadata

    def invalidate_download(self, url: str, reason: str) -> None:
        """Quarantine a URL index and its content after semantic validation fails."""
        index_path = self._index_path(url)
        try:
            metadata = json.loads(index_path.read_text(encoding="utf-8"))
            digest = str(metadata.get("sha256") or "")
            object_path = self._object_path(digest) if len(digest) == 64 else None
            if object_path is not None and object_path.exists():
                self.quarantine_path(object_path, reason)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        if index_path.exists():
            self.quarantine_path(index_path, reason)

    def quarantine_path(self, path: str | Path, reason: str) -> Path | None:
        source = Path(path)
        if not source.exists() and not source.is_symlink():
            return None
        self.quarantine.mkdir(parents=True, exist_ok=True)
        safe_reason = "".join(
            character if character.isalnum() or character in "._-" else "_"
            for character in str(reason or "invalid")
        )
        destination = self.quarantine / (
            f"{int(time.time())}-{safe_reason}-{uuid.uuid4().hex[:8]}-{source.name}"
        )
        try:
            os.replace(source, destination)
            return destination
        except OSError:
            try:
                if source.is_dir() and not source.is_symlink():
                    shutil.copytree(source, destination, symlinks=True)
                    shutil.rmtree(source)
                else:
                    shutil.copy2(source, destination, follow_symlinks=False)
                    source.unlink()
                return destination
            except OSError:
                return None

    @staticmethod
    def build_key(
        service_key: str,
        source_identity: str,
        inputs: Iterable[str | Path] = (),
        toolchain: dict | None = None,
    ) -> str:
        digest = hashlib.sha256()
        digest.update(str(service_key).encode())
        digest.update(b"\0")
        digest.update(str(source_identity).encode())
        digest.update(b"\0")
        digest.update(platform.machine().encode())
        digest.update(b"\0")
        digest.update(platform.platform().encode())
        ignored_directories = {
            ".git",
            ".next",
            ".venv",
            "bin",
            "build",
            "dist",
            "dist-node",
            "node_modules",
            "obj",
            "target",
            "venv",
        }
        for path in sorted(str(Path(item)) for item in inputs):
            digest.update(path.encode())
            digest.update(b"\0")
            if os.path.isfile(path):
                digest.update(sha256_file(path).encode())
            elif os.path.isdir(path):
                input_root = Path(path)
                seen = 0
                for current_root, directories, filenames in os.walk(
                    input_root, followlinks=False
                ):
                    directories[:] = sorted(
                        name
                        for name in directories
                        if name not in ignored_directories
                        and not Path(current_root, name).is_symlink()
                    )
                    for filename in sorted(filenames):
                        candidate = Path(current_root, filename)
                        if candidate.is_symlink() or not candidate.is_file():
                            continue
                        relative = candidate.relative_to(input_root)
                        digest.update(str(relative).encode())
                        digest.update(b"\0")
                        digest.update(sha256_file(candidate).encode())
                        seen += 1
                        if seen >= 100000:
                            digest.update(b"input-file-limit")
                            break
                    if seen >= 100000:
                        break
            else:
                digest.update(b"missing")
        for name, value in sorted((toolchain or {}).items()):
            digest.update(str(name).encode())
            digest.update(b"=")
            digest.update(str(value).encode())
            digest.update(b"\0")
        return digest.hexdigest()

    def _manifest_for_tree(
        self, root: Path, excluded: Iterable[str] = ()
    ) -> list[dict]:
        excluded_names = {str(item).strip("/") for item in excluded if str(item)}
        entries = []
        for current_root, directories, filenames in os.walk(root, followlinks=False):
            relative_root = Path(current_root).relative_to(root)
            for directory in directories:
                if Path(current_root, directory).is_symlink():
                    raise ValueError(
                        "artifact contains an unsupported directory symlink: "
                        f"{relative_root / directory}"
                    )
            directories[:] = sorted(
                directory
                for directory in directories
                if str(relative_root / directory) not in excluded_names
            )
            for filename in sorted(filenames):
                path = Path(current_root, filename)
                relative = path.relative_to(root)
                if any(
                    str(relative) == item or str(relative).startswith(f"{item}/")
                    for item in excluded_names
                ):
                    continue
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    target = os.readlink(path)
                    resolved_target = os.path.normpath(
                        os.path.join(str(relative.parent), target)
                    )
                    if (
                        os.path.isabs(target)
                        or resolved_target == ".."
                        or resolved_target.startswith(f"..{os.sep}")
                    ):
                        raise ValueError(
                            f"artifact contains an escaping symlink: {relative}"
                        )
                    entries.append(
                        {"path": str(relative), "type": "symlink", "target": target}
                    )
                elif stat.S_ISREG(info.st_mode):
                    entries.append(
                        {
                            "path": str(relative),
                            "type": "file",
                            "size": info.st_size,
                            "mode": stat.S_IMODE(info.st_mode),
                            "sha256": sha256_file(path),
                        }
                    )
                else:
                    raise ValueError(
                        f"artifact contains an unsupported file type: {relative}"
                    )
        return entries

    def store_artifact(
        self,
        service_key: str,
        build_key: str,
        source_dir: str | Path,
        *,
        excluded: Iterable[str] = (),
    ) -> dict:
        if not service_key or not all(
            character.isalnum() or character in "_-" for character in service_key
        ):
            raise ValueError("service_key contains unsupported characters")
        if len(build_key) != 64 or any(
            character not in "0123456789abcdef" for character in build_key.lower()
        ):
            raise ValueError("build_key must be a SHA-256 identifier")
        self.ensure()
        source = Path(source_dir).resolve()
        if not source.is_dir():
            raise OSError(f"artifact source does not exist: {source}")
        destination = self.artifacts / service_key / build_key
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        if temporary.exists():
            shutil.rmtree(temporary)
        files_dir = temporary / "files"
        files_dir.mkdir(parents=True)
        excluded_values = tuple(excluded)
        try:
            shutil.copytree(
                source,
                files_dir,
                dirs_exist_ok=True,
                symlinks=True,
                ignore=(
                    shutil.ignore_patterns(*excluded_values)
                    if excluded_values
                    else None
                ),
            )
            manifest = {
                "format": 1,
                "service_key": service_key,
                "build_key": build_key,
                "created_at": int(time.time()),
                "entries": self._manifest_for_tree(files_dir),
            }
            _atomic_json(temporary / "manifest.json", manifest)
        except (OSError, ValueError):
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        with self._lock:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                shutil.rmtree(temporary)
            else:
                os.replace(temporary, destination)
            retention = max(
                1,
                int(_install_cache_config().get("artifact_retention_count", 2)),
            )
            retained = sorted(
                (
                    entry
                    for entry in destination.parent.iterdir()
                    if entry.is_dir() and not entry.is_symlink()
                ),
                key=lambda entry: entry.stat().st_atime,
                reverse=True,
            )
            for stale in retained[retention:]:
                shutil.rmtree(stale, ignore_errors=True)
        return manifest

    def restore_artifact(
        self, service_key: str, build_key: str, destination_dir: str | Path
    ) -> tuple[bool, str | None]:
        if not service_key or not all(
            character.isalnum() or character in "_-" for character in service_key
        ):
            return False, "service_key contains unsupported characters"
        if len(build_key) != 64 or any(
            character not in "0123456789abcdef" for character in build_key.lower()
        ):
            return False, "build_key must be a SHA-256 identifier"
        artifact = self.artifacts / service_key / build_key
        manifest_path = artifact / "manifest.json"
        files_dir = artifact / "files"
        if not artifact.is_dir():
            return False, "artifact not found"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("build_key") != build_key:
                raise ValueError("artifact manifest key mismatch")
            actual_entries = self._manifest_for_tree(files_dir)
            if actual_entries != manifest.get("entries", []):
                raise ValueError("artifact file set does not match its manifest")
            for entry in manifest.get("entries", []):
                relative = Path(str(entry.get("path") or ""))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("artifact contains an unsafe path")
                source = files_dir / relative
                if entry.get("type") == "file":
                    if not source.is_file() or source.is_symlink():
                        raise ValueError(f"artifact file missing: {relative}")
                    if source.stat().st_size != int(entry.get("size", -1)):
                        raise ValueError(f"artifact size mismatch: {relative}")
                    if sha256_file(source) != entry.get("sha256"):
                        raise ValueError(f"artifact digest mismatch: {relative}")
                elif entry.get("type") == "symlink":
                    symlink_target = str(entry.get("target") or "")
                    resolved_target = os.path.normpath(
                        os.path.join(str(relative.parent), symlink_target)
                    )
                    if (
                        os.path.isabs(symlink_target)
                        or resolved_target == ".."
                        or resolved_target.startswith(f"..{os.sep}")
                        or not source.is_symlink()
                        or os.readlink(source) != symlink_target
                    ):
                        raise ValueError(f"artifact symlink mismatch: {relative}")
                else:
                    raise ValueError("artifact manifest contains an unknown entry")
            destination = Path(destination_dir)
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copytree(files_dir, destination, dirs_exist_ok=True, symlinks=True)
            os.utime(artifact, None)
            return True, None
        except (OSError, ValueError, json.JSONDecodeError) as error:
            logger.warning(
                "Quarantining invalid build artifact %s: %s", artifact, error
            )
            self.quarantine_path(artifact, "invalid-artifact")
            return False, str(error)

    def begin_operation(self, process_name: str, stage: str = "resolving") -> str:
        operation_id = uuid.uuid4().hex
        now = time.time()
        try:
            self.ensure()
            with sqlite3.connect(self.telemetry_db, timeout=5) as connection:
                connection.execute(
                    """
                    INSERT INTO install_operations (
                        operation_id, process_name, stage, status, started_at, updated_at
                    ) VALUES (?, ?, ?, 'running', ?, ?)
                    """,
                    (operation_id, process_name, stage, now, now),
                )
        except (OSError, sqlite3.Error) as error:
            logger.debug("Install operation telemetry is unavailable: %s", error)
        return operation_id

    def update_operation(self, operation_id: str, **updates) -> None:
        allowed = {
            "stage",
            "status",
            "cache_hits",
            "cache_misses",
            "downloaded_bytes",
            "rollback_performed",
            "message",
        }
        values = {key: value for key, value in updates.items() if key in allowed}
        values["updated_at"] = time.time()
        assignments = ", ".join(f"{key} = ?" for key in values)
        if not assignments:
            return
        try:
            if not self.telemetry_db.exists():
                return
            with sqlite3.connect(self.telemetry_db, timeout=5) as connection:
                connection.execute(
                    f"UPDATE install_operations SET {assignments} WHERE operation_id = ?",  # noqa: S608
                    [*values.values(), operation_id],
                )
        except sqlite3.Error as error:
            logger.debug("Failed updating install operation telemetry: %s", error)

    @contextmanager
    def operation(self, process_name: str):
        operation_id = self.begin_operation(process_name)
        try:
            yield operation_id
        except Exception as error:
            self.update_operation(
                operation_id,
                status="failed",
                stage="failed",
                message=str(error)[:1000],
            )
            raise

    def recent_operations(self, limit: int = MAX_RECENT_OPERATIONS) -> list[dict]:
        self.ensure()
        bounded = max(1, min(int(limit), MAX_RECENT_OPERATIONS))
        with sqlite3.connect(self.telemetry_db, timeout=5) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM install_operations
                ORDER BY updated_at DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _configured_service_directories() -> set[Path]:
        """Return configured install roots used by older per-service caches."""
        try:
            from utils.config_loader import CONFIG_MANAGER

            config = getattr(CONFIG_MANAGER, "config", {})
        except Exception:
            return set()

        directories: set[Path] = set()

        def collect(value) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if (
                        key in {"config_dir", "install_dir"}
                        and isinstance(nested, str)
                        and nested.startswith("/")
                    ):
                        candidate = Path(nested)
                        if candidate not in {Path("/"), Path("/config")}:
                            directories.add(candidate)
                    collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)

        collect(config)
        return directories

    def _legacy_candidate_specs(self) -> list[dict]:
        """Discover exact cache layouts owned by previous DUMB releases.

        Arbitrary paths are never accepted from the API. Pattern-based entries
        are restricted to the old package-manager bucket naming convention,
        while per-service entries target only package-manager subdirectories.
        """
        candidates: dict[str, dict] = {}

        def add(path: Path, manager: str, label: str) -> None:
            lexical = Path(os.path.abspath(path))
            try:
                resolved = lexical.resolve(strict=False)
            except OSError:
                return
            if resolved == self.root or self.root in resolved.parents:
                return
            candidates.setdefault(
                str(resolved),
                {
                    "path": str(lexical),
                    "resolved_path": str(resolved),
                    "manager": manager,
                    "label": label,
                },
            )

        for parent, manager in (
            (self.legacy_config_root / ".pnpm-store", "pnpm"),
            (self.legacy_config_root / ".bun-cache", "bun"),
        ):
            try:
                if not parent.is_dir() or parent.is_symlink():
                    continue
                for entry in parent.iterdir():
                    if LEGACY_BUCKET_PATTERN.fullmatch(entry.name):
                        add(entry, manager, f"Legacy {manager} project bucket")
            except OSError:
                continue

        add(
            self.legacy_config_root / ".yarn-cache" / "maintainerr",
            "yarn",
            "Legacy Maintainerr Yarn cache",
        )
        add(
            self.legacy_config_root / ".deno" / "cache",
            "deno",
            "Legacy Profilarr Deno cache",
        )

        for directory in self._configured_service_directories():
            add(directory / ".cache" / "pip", "pip", "Legacy per-service pip cache")
            add(
                directory / ".cache" / "pypoetry",
                "poetry",
                "Legacy per-service Poetry cache",
            )
            add(
                directory / ".nuget" / "packages",
                "nuget",
                "Legacy per-service NuGet packages",
            )
        return list(candidates.values())

    def legacy_entries(self) -> list[dict]:
        entries = []
        for spec in self._legacy_candidate_specs():
            path = Path(spec["path"])
            try:
                if path.is_symlink() or not path.exists():
                    continue
                size, files = _directory_size(path)
                if size <= 0 and files <= 0:
                    continue
                entries.append(
                    {
                        "path": spec["path"],
                        "manager": spec["manager"],
                        "label": spec["label"],
                        "bytes": size,
                        "files": files,
                    }
                )
            except OSError:
                continue
        return sorted(entries, key=lambda entry: (entry["manager"], entry["path"]))

    def status(self) -> dict:
        self.ensure()
        namespaces = {}
        for name, path in (
            ("downloads", self.root / "downloads"),
            ("dependencies", self.root / "dependencies"),
            ("artifacts", self.artifacts),
            ("quarantine", self.quarantine),
        ):
            size, files = _directory_size(path)
            namespaces[name] = {"bytes": size, "files": files}
        namespace_bytes = sum(value["bytes"] for value in namespaces.values())
        managed_bytes, managed_files = _directory_size(self.root)
        metadata_bytes = max(0, managed_bytes - namespace_bytes)
        if metadata_bytes:
            namespaces["metadata"] = {
                "bytes": metadata_bytes,
                "files": max(
                    0,
                    managed_files
                    - sum(value["files"] for value in namespaces.values()),
                ),
            }
        legacy_entries = self.legacy_entries()
        legacy_bytes = sum(entry["bytes"] for entry in legacy_entries)
        legacy_files = sum(entry["files"] for entry in legacy_entries)
        reclaimable_bytes = (
            sum(
                namespaces.get(name, {}).get("bytes", 0)
                for name in INSTALL_CACHE_CLEANUP_SCOPES - {"legacy"}
            )
            + legacy_bytes
        )
        return {
            "enabled": install_cache_enabled(),
            "path": str(self.root),
            "configured_path": str(self.requested_root),
            "using_fallback": self.fallback_reason is not None,
            "fallback_reason": self.fallback_reason,
            "managed_bytes": managed_bytes,
            "legacy_bytes": legacy_bytes,
            "legacy_files": legacy_files,
            "total_bytes": managed_bytes + legacy_bytes,
            "reclaimable_bytes": reclaimable_bytes,
            "max_size_bytes": int(
                float(_install_cache_config().get("max_size_gib", 25))
                * 1024
                * 1024
                * 1024
            ),
            "namespaces": namespaces,
            "legacy_entries": legacy_entries,
            "recent_operations": self.recent_operations(),
        }

    def cleanup(self, scopes: Iterable[str]) -> dict:
        requested = {str(scope).strip().lower() for scope in scopes}
        invalid = requested - INSTALL_CACHE_CLEANUP_SCOPES
        if not requested:
            raise ValueError("at least one install-cache cleanup scope is required")
        if invalid:
            raise ValueError(
                "unsupported install-cache cleanup scope: " + ", ".join(sorted(invalid))
            )

        removed_bytes = 0
        removed_files = 0
        removed_entries = []
        errors = []

        def remove_exact(path: Path, label: str, *, require_inside_root: bool) -> None:
            nonlocal removed_bytes, removed_files
            try:
                if not path.exists() and not path.is_symlink():
                    return
                if path.is_symlink():
                    raise OSError("refusing to remove a symlinked cache path")
                if path.is_mount():
                    raise OSError("refusing to recursively remove a mounted cache path")
                resolved = path.resolve(strict=True)
                if require_inside_root and self.root not in resolved.parents:
                    raise OSError("cache namespace resolved outside the managed root")
                size, files = _directory_size(path)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed_bytes += size
                removed_files += files
                removed_entries.append({"scope": label, "bytes": size, "files": files})
            except OSError as error:
                errors.append({"scope": label, "error": str(error)})

        with self._lock:
            current_targets = {
                "downloads": self.root / "downloads",
                "dependencies": self.root / "dependencies",
                "artifacts": self.artifacts,
                "quarantine": self.quarantine,
            }
            for scope in sorted(requested - {"legacy"}):
                remove_exact(current_targets[scope], scope, require_inside_root=True)

            if "legacy" in requested:
                # Re-discover immediately before deletion and match both the
                # lexical and resolved path captured by the allowlisted scan.
                for spec in self._legacy_candidate_specs():
                    path = Path(spec["path"])
                    try:
                        if path.is_symlink() or not path.exists():
                            continue
                        if str(path.resolve(strict=True)) != spec["resolved_path"]:
                            raise OSError("legacy cache path changed during cleanup")
                    except OSError as error:
                        errors.append(
                            {"scope": "legacy", "path": str(path), "error": str(error)}
                        )
                        continue
                    before = removed_bytes
                    remove_exact(path, "legacy", require_inside_root=False)
                    if removed_bytes > before and removed_entries:
                        removed_entries[-1].update(
                            {
                                "path": spec["path"],
                                "manager": spec["manager"],
                            }
                        )
            self.ensure()

        return {
            "scopes": sorted(requested),
            "removed_bytes": removed_bytes,
            "removed_files": removed_files,
            "removed_entries": removed_entries,
            "errors": errors,
        }

    def verify(self) -> dict:
        self.ensure()
        checked = 0
        quarantined = 0
        errors = []
        for prefix in list(self.objects.iterdir()) if self.objects.exists() else []:
            if not prefix.is_dir() or prefix.is_symlink():
                continue
            for candidate in list(prefix.iterdir()):
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                checked += 1
                try:
                    if (
                        len(candidate.name) != 64
                        or sha256_file(candidate) != candidate.name
                    ):
                        raise ValueError("digest mismatch")
                except (OSError, ValueError) as error:
                    if self.quarantine_path(candidate, "verify-failed"):
                        quarantined += 1
                    errors.append({"entry": candidate.name, "error": str(error)})
        return {"checked": checked, "quarantined": quarantined, "errors": errors[:50]}

    def clear_artifacts(self, service_key: str | None = None) -> dict:
        self.ensure()
        target = self.artifacts
        if service_key:
            if not all(
                character.isalnum() or character in "_-" for character in service_key
            ):
                raise ValueError("service_key contains unsupported characters")
            target = self.artifacts / service_key
        bytes_removed, files_removed = _directory_size(target)
        if target.exists():
            shutil.rmtree(target)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        return {
            "service_key": service_key,
            "removed_bytes": bytes_removed,
            "removed_files": files_removed,
        }

    def prune(self, max_size_bytes: int | None = None) -> dict:
        self.ensure()
        configured_limit = int(
            float(_install_cache_config().get("max_size_gib", 25)) * 1024 * 1024 * 1024
        )
        limit = configured_limit if max_size_bytes is None else max(0, max_size_bytes)
        candidates = []
        if self.objects.exists():
            for prefix in self.objects.iterdir():
                if not prefix.is_dir() or prefix.is_symlink():
                    continue
                for path in prefix.iterdir():
                    try:
                        if path.is_file() and not path.is_symlink():
                            info = path.stat()
                            candidates.append((info.st_atime, info.st_size, path))
                    except OSError:
                        continue
        if self.artifacts.exists():
            for service_dir in self.artifacts.iterdir():
                if not service_dir.is_dir() or service_dir.is_symlink():
                    continue
                for artifact in service_dir.iterdir():
                    if not artifact.is_dir() or artifact.is_symlink():
                        continue
                    size, _ = _directory_size(artifact)
                    try:
                        candidates.append((artifact.stat().st_atime, size, artifact))
                    except OSError:
                        continue
        dependencies = self.root / "dependencies"
        if dependencies.exists():
            # Dependency caches are namespaced as manager/version-or-arch.
            # Prune whole reusable buckets rather than individual package
            # manager files, which avoids leaving a syntactically corrupt
            # store. The next install recreates and verifies the bucket.
            for manager in dependencies.iterdir():
                if not manager.is_dir() or manager.is_symlink():
                    continue
                for bucket in manager.iterdir():
                    if not bucket.is_dir() or bucket.is_symlink():
                        continue
                    size, _ = _directory_size(bucket)
                    try:
                        candidates.append((bucket.stat().st_atime, size, bucket))
                    except OSError:
                        continue
        if self.quarantine.exists():
            for path in self.quarantine.iterdir():
                try:
                    size, _ = (
                        _directory_size(path)
                        if path.is_dir()
                        else (path.stat().st_size, 1)
                    )
                    candidates.append((path.stat().st_atime, size, path))
                except OSError:
                    continue
        total, _ = _directory_size(self.root)
        removed = 0
        reclaimed = 0
        for _, size, path in sorted(candidates):
            if total <= limit:
                break
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed += 1
                reclaimed += size
                total -= size
            except OSError:
                continue
        for base in (self.objects, self.artifacts, dependencies, self.quarantine):
            if not base.exists():
                continue
            for root, directories, _ in os.walk(base, topdown=False):
                for directory in directories:
                    try:
                        Path(root, directory).rmdir()
                    except OSError:
                        pass
        return {
            "removed": removed,
            "reclaimed_bytes": reclaimed,
            "remaining_bytes": total,
            "max_size_bytes": limit,
        }


INSTALL_CACHE = InstallCache()
