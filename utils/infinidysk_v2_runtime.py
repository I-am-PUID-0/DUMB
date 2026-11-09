"""Pinned native InfiniDysk runtime. Installation never extracts into persistent data."""

import hashlib
import json
import os
import platform
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request
import urllib.error
from pathlib import Path

from utils.infinidysk_v2_topology import (
    is_v2 as is_v2,
    validate_native_mount_target,
    validate_rclone_topology,
    validate_selected_owners,
)

RELEASE = "v2.0.0-beta.6"
COMMIT = "6363f4a4a3f1bcbc3308142b66ec3e7d7127f853"
ASSETS = {
    "x64": "8d84037baeec5cb05776130acfbd9f999902f53e64cfd4f5b60246f11e5686a8",
    "arm64": "e12af959e43b83f8c89098c0bfcec2ced1c2e9a39289e59b0bc6e220ac81498c",
}
MARKER = ".dumb-native-runtime.json"
AUTH_FILE = ".dumb-native-auth.json"
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_BINARY = 128 * 1024 * 1024


def resolve_v2_data_dir(service, data_root="/data"):
    return str(Path(service.get("v2_data_dir") or Path(data_root) / "infinidysk-v2"))


def runtime_dir(service):
    return Path(service.get("v2_runtime_dir") or "/opt/infinidysk-v2")


def validate_v2_selection(service):
    if service.get("branch_enabled") or service.get("commit_sha"):
        return (
            False,
            "InfiniDysk 2.x currently supports only the pinned official release archive",
        )
    if str(service.get("v2_release_version") or RELEASE) != RELEASE:
        return False, f"Unsupported native release; supported release is {RELEASE}"
    if (
        str(service.get("repo_owner") or "infinidysk").lower() != "infinidysk"
        or str(service.get("repo_name") or "infinidysk").lower() != "infinidysk"
    ):
        return False, "InfiniDysk 2.x archives require the official upstream repository"
    if service.get("mount_backend", "rclone") not in {"rclone", "fuse"}:
        return False, "Unknown InfiniDysk mount backend"
    return True, None


def _directory(path):
    path = Path(path)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise ValueError(
            "Native runtime/data paths must be absolute dedicated directories"
        )
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Native runtime/data paths cannot cross symbolic links")
    if path.exists() and not path.is_dir():
        raise ValueError("Native runtime/data path is not a directory")
    return path


def validate_v2_paths(service, data_root="/data"):
    try:
        binary = _directory(runtime_dir(service))
        data = _directory(resolve_v2_data_dir(service, data_root))
        legacy = Path(os.path.realpath(service.get("config_dir") or "/infinidysk"))
        for first, second in ((binary, data), (binary, legacy), (data, legacy)):
            if first == second or first in second.parents or second in first.parents:
                raise ValueError(
                    "Native program, native data and retained 1.x directories must be separate"
                )
        if binary in {
            Path("/opt"),
            Path("/usr"),
            Path("/tmp"),
            Path(data_root),
        } or data == Path(data_root):
            raise ValueError("Use a dedicated native program/data subdirectory")
        return True, None
    except (OSError, ValueError) as error:
        return False, str(error)


def _private_json(path, value, uid=None, gid=None):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Native private state cannot be a symbolic link")
    fd, temporary = tempfile.mkstemp(prefix=".dumb-native-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            if os.geteuid() == 0 and uid is not None:
                os.fchown(output.fileno(), uid, gid)
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def native_auth(
    service,
    data_root="/data",
    *,
    create=False,
    uid=None,
    gid=None,
    allow_v1_import=False,
):
    """Private controller key; never return it from public metadata/status responses."""
    data = _directory(resolve_v2_data_dir(service, data_root))
    path = data / AUTH_FILE
    if path.exists() or path.is_symlink():
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > 8192
            ):
                raise ValueError(
                    "Native private credentials must be a bounded mode-0600 regular file"
                )
            auth = json.load(source)
        if not isinstance(auth.get("api_key"), str) or len(auth["api_key"]) < 32:
            raise ValueError("Native controller credentials are invalid")
        return auth
    if not create:
        raise ValueError("Native controller credentials have not been prepared")
    _validate_data_bootstrap(
        data,
        allow_v1_import=allow_v1_import,
        native_url=native_database_url(service, data_root),
    )
    data.mkdir(parents=True, exist_ok=True)
    env = service.get("env") or {}
    importing = (data / "db.sqlite").exists() or (data / "db-contract.json").exists()
    auth = {
        "api_key": secrets.token_hex(32),
        "webdav_username": env.get("WEBDAV_USER") or "admin",
        "webdav_password": env.get("WEBDAV_PASSWORD")
        or (None if importing else secrets.token_hex(32)),
    }
    _private_json(path, auth, uid, gid)
    return auth


def _validate_data_bootstrap(data, *, allow_v1_import=False, native_url=None):
    data = Path(data)
    if data in {
        Path("/etc"),
        Path("/var"),
        Path("/var/lib"),
        Path("/usr"),
        Path("/usr/local"),
        Path("/home"),
        Path("/root"),
        Path("/opt"),
        Path("/tmp"),
        Path("/config"),
        Path("/mnt"),
    }:
        raise ValueError("Native data requires a dedicated owned subdirectory")
    private = data / AUTH_FILE
    if (
        data.exists()
        and any(data.iterdir())
        and not private.is_file()
        and not allow_v1_import
    ):
        raise ValueError(
            "Refusing foreign nonempty native data; use the guarded major migration for a retained 1.x copy"
        )
    main, retired = data / "infinidysk.sqlite", data / "infinidysk.sqlite.pre-postgres"
    for evidence in (main, retired):
        if evidence.is_symlink():
            raise ValueError("Native database evidence cannot be a symbolic link")
    native_evidence = main.is_file() or (
        retired.is_file()
        and str(native_url or "").startswith(("postgres://", "postgresql://"))
    )
    if (
        ((data / "db.sqlite").exists() or (data / "db-contract.json").exists())
        and not native_evidence
        and not allow_v1_import
    ):
        raise ValueError(
            "A 1.x import requires the guarded contained candidate executor"
        )


def build_v2_environment(service, data_root="/data", uid=1000, gid=1000):
    auth = native_auth(service, data_root)
    env = {
        str(k): str(v)
        for k, v in (service.get("env") or {}).items()
        if str(k).startswith("INFINIDYSK_")
        or k
        in {
            "TZ",
            "RUST_LOG",
            "PGPASSFILE",
            "PGSSLROOTCERT",
            "PGSSLCERT",
            "PGSSLKEY",
            "PGOPTIONS",
        }
    }
    env.update(
        {
            "INFINIDYSK_CONFIG_DIR": resolve_v2_data_dir(service, data_root),
            "INFINIDYSK_LISTEN": f"0.0.0.0:{int(service.get('frontend_port') or 3000)}",
            "INFINIDYSK__AUTH__API_KEY": auth["api_key"],
            "PUID": str(uid),
            "PGID": str(gid),
            "INFINIDYSK__MOUNT__DIR": service.get("mount_path")
            or "/mnt/debrid/infinidysk",
            "INFINIDYSK__MOUNT__FUSE": (
                "true" if service.get("mount_backend", "rclone") == "fuse" else "false"
            ),
        }
    )
    if auth.get("webdav_password"):
        env["INFINIDYSK__WEBDAV__USER"] = auth["webdav_username"]
        env["INFINIDYSK__WEBDAV__PASSWORD"] = auth["webdav_password"]
    return env


def read_native_credentials(service, data_root="/data"):
    return native_auth(service, data_root)


def _native_database_boot(service, data_root):
    """Read a bounded direct native journal without opening a database."""
    data = _directory(resolve_v2_data_dir(service, data_root))
    path = data / "database.toml"
    saved = {}
    if path.exists() or path.is_symlink():
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
                raise ValueError(
                    "Native database boot state is not a bounded regular file"
                )
            try:
                saved = tomllib.load(source)
            except ValueError:
                raise ValueError("Native database boot state is invalid") from None
    return data, saved


def _selected_database_url(service, data, saved):
    url = (
        (service.get("env") or {}).get("INFINIDYSK_DATABASE_URL")
        or saved.get("url")
        or f"sqlite:{data / 'infinidysk.sqlite'}"
    )
    if not isinstance(url, str) or not url.startswith(
        ("sqlite:", "postgres://", "postgresql://")
    ):
        raise ValueError("Native database boot selection is unsupported")
    return url


def native_database_url(service, data_root="/data"):
    """Read the actual provider, never v1's dormant provider variables."""
    data, saved = _native_database_boot(service, data_root)
    return _selected_database_url(service, data, saved)


def native_database_dependency_urls(service, data_root="/data"):
    """Return actual and pending providers for startup ordering only.

    A copying journal still serves SQLite, but its managed PostgreSQL target
    must start before the native process can prove target identity and resume.
    This does not authorize a job or reinterpret the current health provider.
    """
    data, saved = _native_database_boot(service, data_root)
    urls = [_selected_database_url(service, data, saved)]
    migration = saved.get("migration") or {}
    if not isinstance(migration, dict):
        raise ValueError("Native database migration journal is invalid")
    if migration.get("state") in {"copying", "syncing", "cutover"}:
        target = migration.get("target")
        if not isinstance(target, str) or not target.startswith(
            ("postgres://", "postgresql://")
        ):
            raise ValueError("Native database migration target is unsupported")
        if target not in urls:
            urls.append(target)
    return tuple(urls)


def rclone_remote(service, existing, obscure, data_root="/data"):
    """Adapt the endpoint, keeping a migrated remote's existing obscured secret."""
    auth = native_auth(service, data_root)
    values = {}
    for line in existing or []:
        key, separator, value = str(line).partition("=")
        if separator:
            values[key.strip()] = value.strip()
    password = auth.get("webdav_password")
    if password:
        obscured = obscure(password)
        if not obscured:
            raise ValueError("Could not prepare native WebDAV credentials for rclone")
    else:
        obscured = values.get("pass")
        if not obscured:
            raise ValueError(
                "Migrated WebDAV requires its retained rclone credentials or an explicit WEBDAV_PASSWORD"
            )
    port = int(service.get("frontend_port") or 3000)
    return [
        "type = webdav",
        f"url = http://127.0.0.1:{port}/",
        "vendor = other",
        "pacer_min_sleep = 0",
        f"user = {auth.get('webdav_username') if password else values.get('user', 'admin')}",
        f"pass = {obscured}",
    ]


def native_api_request(
    service, method, path, payload=None, *, timeout=10, data_root="/data"
):
    """Bounded loopback-only API access. Exceptions never contain response secrets."""
    if (
        not isinstance(path, str)
        or not (path.startswith("/api/v1/") or path == "/healthz")
        or any(c in path for c in "?#\\\r\n")
        or ".." in path.split("/")
    ):
        raise ValueError("Unsupported native API path")
    method = str(method).upper()
    if method not in {"GET", "POST", "PUT", "DELETE"}:
        raise ValueError("Unsupported native API method")
    auth = native_auth(service, data_root)
    port = int(service.get("frontend_port") or 3000)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid native API port")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"X-Api-Key": auth["api_key"], "Content-Type": "application/json"},
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("Native API response exceeds supported size")
            body = json.loads(raw) if raw.strip() else None
            if body is not None and not isinstance(body, (dict, list)):
                raise ValueError("Native API returned an unsupported JSON response")
            return {"status": response.code, "body": body}
    except (OSError, ValueError, urllib.error.URLError):
        raise ValueError(
            "Native API request failed; inspect the private application logs"
        ) from None


def installed_v2_version(service):
    try:
        root = _directory(runtime_dir(service))
        marker = json.loads((root / MARKER).read_text())
        binary = root / "infinidysk"
        if (
            binary.is_symlink()
            or not binary.is_file()
            or not os.access(binary, os.X_OK)
        ):
            return None
        if (root / "version.txt").read_text().strip().lstrip("v") != RELEASE.lstrip(
            "v"
        ) or (root / "commit.txt").read_text().strip() != COMMIT:
            return None
        if (
            marker.get("commit") != COMMIT
            or marker.get("binary_sha256")
            != hashlib.sha256(binary.read_bytes()).hexdigest()
        ):
            return None
        return RELEASE
    except (OSError, ValueError, TypeError):
        return None


def _download_archive(url, destination):
    if not url.startswith(
        f"https://github.com/infinidysk/infinidysk/releases/download/{RELEASE}/"
    ):
        raise ValueError("Unsupported native archive URL")
    with (
        urllib.request.urlopen(url, timeout=60) as response,  # noqa: S310
        open(destination, "wb") as output,
    ):
        # GitHub release redirects are HTTPS; never accept a downgraded transport.
        if not response.geturl().startswith("https://"):
            raise ValueError("Native archive download changed to an insecure transport")
        remaining = MAX_ARCHIVE
        while chunk := response.read(min(1024 * 1024, remaining + 1)):
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError("Native archive exceeds the supported size")
            output.write(chunk)


def install_v2_runtime(process_handler, process_name, service):
    del process_name
    from utils.config_loader import CONFIG_MANAGER

    data_root = CONFIG_MANAGER.get("data_root") or "/data"
    for valid, error in (
        validate_v2_selection(service),
        validate_v2_paths(service, data_root),
    ):
        if not valid:
            return False, error
    if installed_v2_version(service):
        return True, None
    target = runtime_dir(service)
    if target.exists() and any(target.iterdir()):
        return False, "Refusing to replace an unverified native runtime directory"
    machine = platform.machine().lower()
    architecture = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if platform.system() != "Linux" or architecture not in ASSETS:
        return False, "Native InfiniDysk supports Linux x64 and arm64 only"
    try:
        libc, version = platform.libc_ver()
        if libc != "glibc" or tuple(int(n) for n in version.split(".")[:2]) < (2, 39):
            return False, "Native InfiniDysk requires glibc 2.39 or newer"
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".infinidysk-v2-", dir=target.parent
        ) as staging:
            Path(staging).chmod(0o755)
            archive = Path(staging) / "runtime.tar.gz"
            name = f"infinidysk-{RELEASE}-linux-{architecture}"
            _download_archive(
                f"https://github.com/infinidysk/infinidysk/releases/download/{RELEASE}/{name}.tar.gz",
                archive,
            )
            if hashlib.sha256(archive.read_bytes()).hexdigest() != ASSETS[architecture]:
                raise ValueError(
                    "Native archive SHA-256 does not match the pinned release"
                )
            candidate = Path(staging) / "candidate"
            candidate.mkdir(mode=0o755)
            expected = {"infinidysk", "LICENSE", "version.txt", "commit.txt"}
            with tarfile.open(archive, "r:gz") as package:
                seen = set()
                for member in package:
                    relative = Path(member.name)
                    if member.isdir() and str(relative).rstrip("/") == name:
                        continue
                    if (
                        relative.parent != Path(name)
                        or relative.name not in expected
                        or not member.isfile()
                        or member.size > MAX_BINARY
                        or relative.name in seen
                    ):
                        raise ValueError(
                            "Native archive has an unexpected path, link or duplicate member"
                        )
                    seen.add(relative.name)
                    with (
                        package.extractfile(member) as source,
                        open(candidate / relative.name, "wb") as output,
                    ):
                        shutil.copyfileobj(source, output)
                if seen != expected:
                    raise ValueError("Native archive is missing required files")
            if (candidate / "commit.txt").read_text().strip() != COMMIT or (
                candidate / "version.txt"
            ).read_text().strip().lstrip("v") != RELEASE.lstrip("v"):
                raise ValueError(
                    "Native archive version/commit does not match the pinned release"
                )
            binary = candidate / "infinidysk"
            binary.chmod(0o755)
            probe = subprocess.run(
                [str(binary), "openapi"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=True,
                env={"PATH": os.defpath},
                cwd=candidate,
                **(
                    {
                        "user": int((CONFIG_MANAGER.get("puid") or 1000)),
                        "group": int((CONFIG_MANAGER.get("pgid") or 1000)),
                        "extra_groups": [],
                    }
                    if os.geteuid() == 0
                    else {}
                ),
            )
            schema = json.loads(probe.stdout)
            if (
                not isinstance(schema.get("paths"), dict)
                or "/api/v1/upgrade/status" not in schema["paths"]
            ):
                raise ValueError("Native executable does not expose the supported API")
            _private_json(
                candidate / MARKER,
                {
                    "release": RELEASE,
                    "commit": COMMIT,
                    "archive_sha256": ASSETS[architecture],
                    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                },
            )
            if target.exists():
                target.rmdir()
            os.replace(candidate, target)
        process_handler.logger.info(
            "Installed verified InfiniDysk native runtime %s", RELEASE
        )
        return True, None
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
        return False, f"Native runtime installation failed: {error}"


def prepare_v2_launch(
    service,
    uid,
    gid,
    *,
    data_root="/data",
    rclone_instances=None,
    allow_v1_import=False,
):
    for valid, error in (
        validate_v2_selection(service),
        validate_v2_paths(service, data_root),
    ):
        if not valid:
            return False, error
    if not installed_v2_version(service):
        return False, "Native runtime is missing or its pinned provenance changed"
    try:
        if rclone_instances is not None:
            valid, error = validate_selected_owners(service, rclone_instances)
            if not valid:
                return False, error
        native_database_url(service, data_root)
        _validate_data_bootstrap(
            resolve_v2_data_dir(service, data_root),
            allow_v1_import=allow_v1_import,
            native_url=native_database_url(service, data_root),
        )
        private = Path(resolve_v2_data_dir(service, data_root)) / AUTH_FILE
        if private.exists() or private.is_symlink():
            native_auth(service, data_root)
        if service.get("mount_backend", "rclone") == "fuse":
            valid, error = validate_native_mount_target(
                service.get("mount_path") or "/mnt/debrid/infinidysk"
            )
            if not valid:
                return False, error
            if not Path("/dev/fuse").exists() or not shutil.which("fusermount3"):
                return False, "Native FUSE requires /dev/fuse and fusermount3"
            if uid != 0 and not any(
                line.split("#", 1)[0].strip() == "user_allow_other"
                for line in Path("/etc/fuse.conf").read_text().splitlines()
            ):
                return (
                    False,
                    "Native FUSE with an unprivileged owner requires user_allow_other in /etc/fuse.conf",
                )
        elif rclone_instances is not None:
            valid, error = validate_rclone_topology(service, rclone_instances)
            if not valid:
                return False, error
        if service.get("mount_backend", "rclone") == "fuse":
            mount = Path(service.get("mount_path") or "/mnt/debrid/infinidysk")
            mount.mkdir(parents=True, exist_ok=True)
            if os.geteuid() == 0:
                os.chown(mount, uid, gid, follow_symlinks=False)
        data = _directory(resolve_v2_data_dir(service, data_root))
        data.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(data, uid, gid, follow_symlinks=False)
        native_auth(
            service,
            data_root,
            create=True,
            uid=uid,
            gid=gid,
            allow_v1_import=allow_v1_import,
        )
        return True, None
    except (OSError, ValueError) as error:
        return False, str(error)


def configure_v2_runtime(process_handler, process_name, service):
    del process_handler, process_name
    from utils.config_loader import CONFIG_MANAGER

    uid, gid = int((CONFIG_MANAGER.get("puid") or 1000)), int(
        (CONFIG_MANAGER.get("pgid") or 1000)
    )
    data_root = CONFIG_MANAGER.get("data_root") or "/data"
    valid, error = prepare_v2_launch(
        service,
        uid,
        gid,
        data_root=data_root,
        rclone_instances=(CONFIG_MANAGER.get("rclone") or {}).get("instances", {}),
    )
    if not valid:
        return False, error
    service["command"] = [str(runtime_dir(service) / "infinidysk")]
    # Keep secrets in private native state. ProcessHandler builds this environment
    # immediately before launch, rather than persisting credentials in DUMB config.
    return True, None
