"""Build and activate official Audiobookshelf sources outside persistent data."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import stat
import subprocess
import tempfile

from utils.download import Downloader
from utils.install_cache import InstallCache, _repair_cache_tree, install_cache_root
from utils.transactional_install import DirectoryReleaseTransaction

AUDIOBOOKSHELF_REPO_OWNER = "advplyr"
AUDIOBOOKSHELF_REPO_NAME = "audiobookshelf"
AUDIOBOOKSHELF_ROUTER_BASE = "/audiobookshelf"
_VERSION = re.compile(r"v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?)")
_SHA = re.compile(r"[0-9a-f]{40}")
_MARKER = ".dumb-audiobookshelf.json"


class AudiobookshelfInstallError(RuntimeError):
    pass


def _valid_branch(branch: str) -> bool:
    """Accept branch names without Git revision expressions or ambiguous refs."""
    return bool(
        branch
        and len(branch) <= 255
        and not branch.startswith(("-", "/", "."))
        and not branch.endswith(("/", "."))
        and not any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 for char in branch
        )
        and not any(char in branch for char in "~^:?*[\\")
        and ".." not in branch
        and "@{" not in branch
        and branch != "@"
        and all(
            part and not part.startswith(".") and not part.endswith(".lock")
            for part in branch.split("/")
        )
    )


def audiobookshelf_install_selector(config: dict) -> str:
    for key, expected in (
        ("repo_owner", AUDIOBOOKSHELF_REPO_OWNER),
        ("repo_name", AUDIOBOOKSHELF_REPO_NAME),
    ):
        if str(config.get(key) or expected).lower() != expected.lower():
            raise AudiobookshelfInstallError(
                "Audiobookshelf installation uses its official advplyr/audiobookshelf repository."
            )
    commit = str(config.get("commit_sha") or "").strip().lower()
    if commit:
        if not _SHA.fullmatch(commit):
            raise AudiobookshelfInstallError(
                "Audiobookshelf commit_sha must be a full 40-character Git commit SHA."
            )
        return f"commit:{commit}"
    if config.get("branch_enabled"):
        branch = str(config.get("branch") or "")
        if not _valid_branch(branch):
            raise AudiobookshelfInstallError(
                "Audiobookshelf branch must be a valid branch name such as master "
                "or feature/example, without Git revision expressions."
            )
        return f"branch:{branch}"
    if not config.get("release_version_enabled"):
        return "latest"
    selector = str(config.get("release_version") or "").strip()
    if selector.lower() == "latest":
        return "latest"
    if not _VERSION.fullmatch(selector):
        raise AudiobookshelfInstallError(
            "Audiobookshelf release_version must be latest or a published release "
            "tag such as v2.37.1."
        )
    return selector


def _read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _version(runtime: Path) -> str:
    package = _read_json(runtime / "package.json")
    version = str(package.get("version") or "")
    return (
        version
        if package.get("name") == "audiobookshelf" and _VERSION.fullmatch(version)
        else ""
    )


def _metadata(runtime: Path) -> dict:
    return _read_json(runtime / _MARKER)


def _source_kind(metadata: dict) -> str:
    # Existing release installations predate explicit source identity.
    return metadata.get("source_kind", "release")


def _display_version(metadata: dict) -> str:
    kind = _source_kind(metadata)
    commit = str(metadata.get("commit_sha") or "")
    if kind == "branch":
        return f"{metadata.get('branch')}-{commit[:12]}"
    if kind == "commit":
        return f"commit-{commit[:12]}"
    return str(metadata.get("version") or "")


def _valid_source_metadata(metadata: dict) -> bool:
    version = str(metadata.get("version") or "")
    commit = str(metadata.get("commit_sha") or "")
    selector = str(metadata.get("install_selector") or "")
    package_version = _VERSION.fullmatch(version)
    if not (
        package_version
        and package_version.group(1) == version
        and _SHA.fullmatch(commit)
        and metadata.get("node_major") == 24
    ):
        return False
    kind = _source_kind(metadata)
    if kind == "release":
        tag = _VERSION.fullmatch(str(metadata.get("release_tag") or ""))
        selected = _VERSION.fullmatch(selector)
        return bool(
            tag
            and tag.group(1) == version
            and (selector == "latest" or (selected and selected.group(1) == version))
        )
    if metadata.get("release_tag"):
        return False
    if kind == "branch":
        branch = str(metadata.get("branch") or "")
        return _valid_branch(branch) and selector == f"branch:{branch}"
    if kind == "commit":
        return not metadata.get("branch") and selector == f"commit:{commit}"
    return False


def _recognized_runtime(root: Path) -> bool:
    """Identify a controller-owned installation, including a repairable one."""
    try:
        root_info = root.lstat()
        marker_info = (root / _MARKER).lstat()
    except OSError:
        return False
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.geteuid()
        or root_info.st_mode & 0o022
        or not stat.S_ISREG(marker_info.st_mode)
        or marker_info.st_uid != os.geteuid()
        or marker_info.st_mode & 0o022
    ):
        return False
    metadata = _metadata(root)
    if not _valid_source_metadata(metadata):
        return False
    version = str(metadata["version"])
    package_path = root / "package.json"
    if package_path.is_symlink():
        return False
    package = _read_json(package_path)
    if package and (
        package.get("name") != "audiobookshelf"
        or (package.get("version") and str(package["version"]) != version)
    ):
        return False
    return True


def audiobookshelf_runtime_owned(runtime: str | Path) -> bool:
    """Recognize a controller-owned installation even when its build needs repair."""
    return _recognized_runtime(Path(runtime))


def audiobookshelf_runtime_ready(runtime: str | Path) -> bool:
    root = Path(runtime)
    if not _recognized_runtime(root):
        return False
    metadata = _metadata(root)
    version = _version(root)
    native = root / "node_modules/sqlite3"
    native_bindings = [
        native / "build/Release/node_sqlite3.node",
        *native.glob("lib/binding/**/node_sqlite3.node"),
    ]
    return bool(
        version
        and metadata.get("version") == version
        and metadata.get("node_major") == 24
        and _SHA.fullmatch(str(metadata.get("commit_sha") or ""))
        and all(
            path.is_file() and not path.is_symlink()
            for path in (
                root / "dist-server/index.js",
                root / "client/dist/index.html",
                root / "node_modules/sqlite3/package.json",
            )
        )
        and any(path.is_file() for path in native_bindings)
    )


def audiobookshelf_runtime_matches_selection(
    runtime: str | Path, selector: str
) -> bool:
    root = Path(runtime)
    if not audiobookshelf_runtime_ready(root):
        return False
    metadata = _metadata(root)
    kind = _source_kind(metadata)
    if selector.startswith("commit:"):
        return bool(
            kind == "commit"
            and metadata.get("commit_sha") == selector.removeprefix("commit:")
        )
    if selector.startswith("branch:"):
        return bool(
            kind == "branch"
            and metadata.get("branch") == selector.removeprefix("branch:")
        )
    if kind != "release":
        return False
    if selector == "latest":
        return True
    match = _VERSION.fullmatch(str(selector))
    return bool(match and _version(root) == match.group(1))


def _resolve_release(downloader: Downloader, selector: str) -> dict:
    tag = selector
    if selector == "latest":
        tag, error = downloader.get_latest_release(
            AUDIOBOOKSHELF_REPO_OWNER, AUDIOBOOKSHELF_REPO_NAME, nightly=False
        )
        if not tag:
            raise AudiobookshelfInstallError(
                error or "Cannot resolve the latest Audiobookshelf release."
            )
    match = _VERSION.fullmatch(str(tag or ""))
    if not match:
        raise AudiobookshelfInstallError(
            "Audiobookshelf returned an invalid release tag."
        )
    release, error = downloader.fetch_github_release_info(
        AUDIOBOOKSHELF_REPO_OWNER, AUDIOBOOKSHELF_REPO_NAME, tag
    )
    if (
        not release
        or release.get("draft")
        or release.get("tag_name") != tag
        or (selector == "latest" and release.get("prerelease"))
    ):
        raise AudiobookshelfInstallError(
            error or "The requested Audiobookshelf release is not published."
        )
    commit, error = downloader.get_ref_commit_sha(
        AUDIOBOOKSHELF_REPO_OWNER, AUDIOBOOKSHELF_REPO_NAME, f"refs/tags/{tag}"
    )
    if not _SHA.fullmatch(str(commit or "")):
        raise AudiobookshelfInstallError(
            error or "Cannot resolve the Audiobookshelf release commit."
        )
    return {
        "source_kind": "release",
        "release_tag": tag,
        "version": match.group(1),
        "commit_sha": commit,
    }


def _source_package_version(downloader: Downloader, commit: str) -> str:
    # GitHub serves the immutable file with the raw media type. Only official
    # API URLs receive the configured GitHub credentials.
    url = (
        f"https://api.github.com/repos/{AUDIOBOOKSHELF_REPO_OWNER}"
        f"/{AUDIOBOOKSHELF_REPO_NAME}/contents/package.json?ref={commit}"
    )
    headers = {**downloader.get_headers(), "Accept": "application/vnd.github.raw+json"}
    response = downloader.fetch_with_retries(url, headers)
    try:
        package = (
            response.json()
            if response is not None and response.status_code == 200
            else {}
        )
    except ValueError:
        package = {}
    version = str(package.get("version") or "") if isinstance(package, dict) else ""
    match = _VERSION.fullmatch(version)
    if not (
        isinstance(package, dict)
        and package.get("name") == "audiobookshelf"
        and match
        and match.group(1) == version
    ):
        raise AudiobookshelfInstallError(
            "Cannot read a valid Audiobookshelf package version at the selected source commit."
        )
    return version


def _resolve_target(downloader: Downloader, selector: str) -> dict:
    if selector.startswith("commit:"):
        kind = "commit"
        branch = ""
        ref = selector.removeprefix("commit:")
    elif selector.startswith("branch:"):
        kind = "branch"
        branch = selector.removeprefix("branch:")
        # A tag with the same name must never satisfy a requested branch.
        ref = f"refs/heads/{branch}"
    else:
        return _resolve_release(downloader, selector)
    commit, error = downloader.get_ref_commit_sha(
        AUDIOBOOKSHELF_REPO_OWNER, AUDIOBOOKSHELF_REPO_NAME, ref
    )
    if not _SHA.fullmatch(str(commit or "")) or (kind == "commit" and commit != ref):
        raise AudiobookshelfInstallError(
            error or "Cannot resolve the requested Audiobookshelf source commit."
        )
    return {
        "source_kind": kind,
        "branch": branch,
        "release_tag": "",
        "version": _source_package_version(downloader, commit),
        "commit_sha": commit,
    }


def audiobookshelf_target_status(
    config: dict, *, downloader: Downloader | None = None
) -> dict:
    selector = audiobookshelf_install_selector(config)
    runtime = Path(config.get("runtime_dir") or "/opt/audiobookshelf")
    target = _resolve_target(downloader or Downloader(), selector)
    ready = audiobookshelf_runtime_ready(runtime)
    current = _metadata(runtime) if ready else {}
    return {
        "selector": selector,
        "source_kind": target["source_kind"],
        "branch": target.get("branch", ""),
        "current_source_kind": _source_kind(current) if ready else "",
        "current_version": _display_version(current) if ready else "",
        "available_version": _display_version(target),
        "current_package_version": current.get("version", ""),
        "available_package_version": target["version"],
        "current_commit_sha": current.get("commit_sha", ""),
        "installed": bool(
            ready
            and audiobookshelf_runtime_matches_selection(runtime, selector)
            and current.get("version") == target["version"]
            and current.get("commit_sha") == target["commit_sha"]
        ),
        "release_tag": target["release_tag"],
        "commit_sha": target["commit_sha"],
    }


def _stop_group(process: subprocess.Popen) -> None:
    """Reap the command and terminate any children left in its private session."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _run(command: list[str], cwd: Path, env: dict, *, timeout: int = 1200) -> str:
    with tempfile.TemporaryFile(mode="w+b") as output:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                raise AudiobookshelfInstallError(
                    f"Audiobookshelf build command {command[0]} timed out."
                ) from error
        finally:
            _stop_group(process)
        output.seek(0, os.SEEK_END)
        size = output.tell()
        output.seek(max(0, size - 16384))
        text = output.read().decode("utf-8", errors="replace")
        if code:
            raise AudiobookshelfInstallError(
                f"Audiobookshelf {' '.join(command)} failed:\n{text}"
            )
        return text


def _check_node(env: dict) -> None:
    version = _run(["node", "--version"], Path("/tmp"), env, timeout=30).strip()
    if not re.fullmatch(r"v24\.\d+\.\d+", version):
        raise AudiobookshelfInstallError(
            "Audiobookshelf requires Node.js 24. Update the DUMB image before installing."
        )


def validate_audiobookshelf_node_runtime() -> None:
    """Check the managed Node runtime before configure-only startup as well."""
    _check_node(os.environ.copy())


def _safe_runtime_path(config: dict) -> Path:
    configured = Path(config.get("runtime_dir") or "/opt/audiobookshelf")
    if configured.is_symlink():
        raise AudiobookshelfInstallError(
            "Audiobookshelf runtime_dir cannot be a symbolic link."
        )
    runtime = configured.resolve()
    if runtime == Path("/"):
        raise AudiobookshelfInstallError("Audiobookshelf runtime_dir cannot be root.")
    for field, default in (
        ("config_dir", "/audiobookshelf/config"),
        ("metadata_dir", "/audiobookshelf/metadata"),
    ):
        data = Path(config.get(field) or default).resolve()
        if runtime == data or runtime in data.parents or data in runtime.parents:
            raise AudiobookshelfInstallError(
                "Audiobookshelf runtime, config and metadata must use separate directories."
            )
    if runtime.exists():
        info = runtime.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise AudiobookshelfInstallError(
                "Audiobookshelf runtime_dir must be a directory."
            )
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise AudiobookshelfInstallError(
                "An existing Audiobookshelf runtime directory must be owned and "
                "writable only by the DUMB controller."
            )
        if any(runtime.iterdir()) and not _recognized_runtime(runtime):
            raise AudiobookshelfInstallError(
                "Audiobookshelf runtime_dir contains unrelated or unrecognized "
                "data. Choose an empty controller-owned directory; existing "
                "contents will not be replaced."
            )
    runtime.parent.mkdir(parents=True, exist_ok=True)
    current = runtime.parent
    while True:
        info = current.stat()
        system_tmp = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        if info.st_uid not in {0, os.geteuid()} or (
            stat.S_IMODE(info.st_mode) & 0o022 and not system_tmp
        ):
            raise AudiobookshelfInstallError(
                "Audiobookshelf runtime requires controller-owned parent directories "
                "without group or other write permission."
            )
        if current.parent == current:
            break
        current = current.parent
    return runtime


def _npm_cache() -> str:
    """Reuse the controller cache without accepting service-writable roots."""
    cache = InstallCache(root=install_cache_root())
    cache.ensure()
    path = cache.root
    try:
        for part in ("dependencies", "npm", "node24", platform.machine().lower()):
            path = path / part
            if path.is_symlink():
                raise OSError("build cache namespace is a symbolic link")
            path.mkdir(mode=0o700, exist_ok=True)
            _repair_cache_tree(path, os.geteuid(), os.getegid())
            path.chmod(0o700)
    except OSError as error:
        raise AudiobookshelfInstallError(
            "Cannot safely prepare the Audiobookshelf npm build cache."
        ) from error
    return str(path)


def _build_candidate(root: Path, env: dict, expected_version: str) -> None:
    if _version(root) != expected_version:
        raise AudiobookshelfInstallError(
            "Audiobookshelf source package version does not match the selected target."
        )
    for relative in (
        "package-lock.json",
        "client/package.json",
        "client/package-lock.json",
    ):
        if not (root / relative).is_file():
            raise AudiobookshelfInstallError("Audiobookshelf source is incomplete.")
    if (
        str(_read_json(root / "client/package.json").get("version") or "")
        != expected_version
    ):
        raise AudiobookshelfInstallError(
            "Audiobookshelf server and client versions do not match."
        )
    _run(["npm", "ci", "--include=dev", "--no-audit", "--no-fund"], root, env)
    _run(["npm", "run", "build:server"], root, env)
    _run(
        ["npm", "ci", "--include=dev", "--no-audit", "--no-fund"],
        root / "client",
        env,
    )
    _run(["npm", "run", "generate"], root / "client", env)
    _run(["npm", "prune", "--omit=dev", "--no-audit", "--no-fund"], root, env)
    validation = (
        "const sqlite=require('sqlite3');"
        "const db=new sqlite.Database(':memory:');"
        "db.get('SELECT 1 AS healthy',(error,row)=>{"
        "if(error||row.healthy!==1){process.exitCode=1;}db.close();});"
    )
    _run(["node", "-e", validation], root, env, timeout=30)
    _run(["node", "--check", "dist-server/index.js"], root, env, timeout=30)
    for relative in ("dist-server/index.js", "client/dist/index.html"):
        if not (root / relative).is_file():
            raise AudiobookshelfInstallError(
                "Audiobookshelf build did not produce the server and web client."
            )
    for relative in ("client/node_modules", "client/.nuxt", "node-compile-cache"):
        path = root / relative
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)


def install_audiobookshelf_runtime(
    config: dict,
    *,
    before_activate=None,
    force: bool = False,
    downloader: Downloader | None = None,
) -> dict:
    selector = audiobookshelf_install_selector(config)
    runtime = _safe_runtime_path(config)
    env = os.environ.copy()
    env.update(
        {
            "NODE_ENV": "production",
            "CYPRESS_INSTALL_BINARY": "0",
            "NUXT_TELEMETRY_DISABLED": "1",
            "ROUTER_BASE_PATH": AUDIOBOOKSHELF_ROUTER_BASE,
            "npm_config_cache": _npm_cache(),
        }
    )
    _check_node(env)
    if platform.system() != "Linux" or platform.machine().lower() not in {
        "x86_64",
        "amd64",
        "aarch64",
        "arm64",
    }:
        raise AudiobookshelfInstallError(
            "Audiobookshelf currently supports Linux amd64 and arm64 runtimes."
        )
    if not force and audiobookshelf_runtime_matches_selection(runtime, selector):
        metadata = _metadata(runtime)
        return {
            **metadata,
            "runtime_dir": str(runtime),
            "install_selector": selector,
            "display_version": _display_version(metadata),
            "source_kind": _source_kind(metadata),
        }
    client = downloader or Downloader()
    release = _resolve_target(client, selector)
    transaction = DirectoryReleaseTransaction(
        str(runtime), config.get("process_name") or "Audiobookshelf"
    )
    candidate = Path(transaction.prepare())
    try:
        candidate.chmod(0o700)
        transaction.mark_building()
        archive_url = (
            f"https://github.com/{AUDIOBOOKSHELF_REPO_OWNER}/{AUDIOBOOKSHELF_REPO_NAME}"
            f"/archive/{release['commit_sha']}.tar.gz"
        )
        success, error = client.download_and_extract(
            archive_url,
            str(candidate),
            zip_folder_name=f"{AUDIOBOOKSHELF_REPO_NAME}-{release['commit_sha']}",
        )
        if not success:
            raise AudiobookshelfInstallError(
                error or "Cannot download the Audiobookshelf source."
            )
        _build_candidate(candidate, env, release["version"])
        metadata = {
            **release,
            "install_selector": selector,
            "node_major": 24,
            "router_base_path": AUDIOBOOKSHELF_ROUTER_BASE,
        }
        metadata["display_version"] = _display_version(metadata)
        (candidate / _MARKER).write_text(json.dumps(metadata), encoding="utf-8")
        (candidate / _MARKER).chmod(0o644)
        (candidate / "version.txt").write_text(
            metadata["display_version"] + "\n", encoding="utf-8"
        )
        (candidate / "install-selector.txt").write_text(
            selector + "\n", encoding="utf-8"
        )
        (candidate / "source-commit.txt").write_text(
            release["commit_sha"] + "\n", encoding="utf-8"
        )
        if not audiobookshelf_runtime_ready(candidate):
            raise AudiobookshelfInstallError(
                "Audiobookshelf candidate runtime is incomplete."
            )
        candidate.chmod(0o755)
        if before_activate is not None:
            before_activate()
        transaction.activate()
    except Exception:
        if transaction.activated:
            transaction.rollback()
        raise
    finally:
        if not transaction.activated:
            transaction.abandon()
    # The validated replacement is committed before old-backup cleanup. A
    # partial cleanup must never restore an already-partially-deleted backup.
    try:
        transaction.commit()
    except OSError as error:
        raise AudiobookshelfInstallError(
            "Audiobookshelf runtime was activated, but previous-runtime cleanup "
            "did not complete. Preserve the transaction recovery files and "
            "resolve cleanup permissions before retrying."
        ) from error
    return {**metadata, "runtime_dir": str(runtime)}
