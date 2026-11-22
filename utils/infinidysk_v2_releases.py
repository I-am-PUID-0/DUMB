"""Official v2 artifacts for fresh installations and native release updates.

Migration qualification remains pinned in infinidysk_v2_runtime. Release
discovery and staging do not start services, modify databases or replace the
installed runtime; the lifecycle owner publishes a staged directory separately.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
from urllib.parse import quote, urlsplit

from utils.infinidysk_release_controls import selected_release as configured_release

API = "https://api.github.com/repos/infinidysk/infinidysk"
DOWNLOAD = "https://github.com/infinidysk/infinidysk/releases/download"
FORMAT = "official-v2-release-v1"
MAX_JSON = 8 * 1024 * 1024
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_BINARY = 128 * 1024 * 1024
_VERSION = re.compile(
    r"v?2\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)
_SHA = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_PLAN_FIELDS = {
    "release",
    "commit",
    "architecture",
    "asset_name",
    "archive_url",
    "archive_sha256",
    "archive_size",
}
_MARKER_FIELDS = {
    "format",
    "release",
    "commit",
    "architecture",
    "archive_sha256",
    "binary_sha256",
}


class NativeReleaseError(ValueError):
    """A bounded, credential-free explanation of refused native release work."""


def release_tag(value):
    """Validate exact v2 SemVer, including numeric prerelease identifier rules."""
    if not isinstance(value, str) or len(value) > 128:
        raise NativeReleaseError("Choose an official InfiniDysk 2 release or latest.")
    parsed = _VERSION.fullmatch(value)
    if not parsed or any(
        part.isascii() and part.isdigit() and len(part) > 1 and part.startswith("0")
        for part in (parsed[3] or "").split(".")
    ):
        raise NativeReleaseError("Choose an official InfiniDysk 2 release or latest.")
    return value if value.startswith("v") else "v" + value


def _version_key(value):
    parsed = _VERSION.fullmatch(release_tag(value))
    prerelease = parsed[3]
    return (
        int(parsed[1]),
        int(parsed[2]),
        1 if prerelease is None else 0,
        tuple(
            (0, int(part)) if part.isdigit() else (1, part)
            for part in (prerelease or "").split(".")
        ),
    )


def compare_releases(first, second):
    """Compare validated v2 SemVer precedence; build metadata has no ordering."""
    left, right = _version_key(first), _version_key(second)
    return (left > right) - (left < right)


def selected_release(service):
    try:
        selected = configured_release(service)
    except ValueError as error:
        raise NativeReleaseError(str(error)) from None
    return "latest" if selected == "latest" else release_tag(selected)


def validate_service(service):
    if service.get("branch_enabled") or service.get("commit_sha"):
        raise NativeReleaseError(
            "Native InfiniDysk updates support official release archives; branch and commit source builds are unavailable."
        )
    if (
        str(service.get("repo_owner") or "infinidysk").lower() != "infinidysk"
        or str(service.get("repo_name") or "infinidysk").lower() != "infinidysk"
    ):
        raise NativeReleaseError(
            "Native InfiniDysk updates require the official upstream repository."
        )
    if service.get("mount_backend", "rclone") not in {"rclone", "fuse"}:
        raise NativeReleaseError("Unknown InfiniDysk mount backend.")
    selected_release(service)


def architecture():
    result = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(platform.machine().lower())
    if platform.system() != "Linux" or result is None:
        raise NativeReleaseError("Native InfiniDysk supports Linux x64 and arm64 only.")
    return result


def _json(path):
    # Paths are constructed only from validated v2 tags or hexadecimal object IDs.
    url = API + path
    request = urllib.request.Request(  # noqa: S310 - fixed official HTTPS API
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "DUMB-native-release",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            if (
                urlsplit(response.geturl()).scheme != "https"
                or urlsplit(response.geturl()).netloc != "api.github.com"
            ):
                raise NativeReleaseError("Official release metadata changed origin.")
            raw = response.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise NativeReleaseError(
                    "Official release metadata exceeds its supported size."
                )
            return json.loads(raw)
    except (OSError, ValueError):
        raise NativeReleaseError(
            "Official InfiniDysk release metadata is unavailable or invalid."
        ) from None


def _tag_commit(tag):
    ref = _json("/git/ref/tags/" + quote(tag, safe=""))
    if not isinstance(ref, dict) or ref.get("ref") != "refs/tags/" + tag:
        raise NativeReleaseError("The official release tag could not be verified.")
    obj = ref.get("object")
    seen = set()
    for _ in range(5):
        if (
            not isinstance(obj, dict)
            or not isinstance(obj.get("sha"), str)
            or not _COMMIT.fullmatch(obj["sha"])
        ):
            break
        if obj.get("type") == "commit":
            return obj["sha"]
        if obj.get("type") != "tag" or obj["sha"] in seen:
            break
        seen.add(obj["sha"])
        annotated = _json("/git/tags/" + obj["sha"])
        if not isinstance(annotated, dict) or annotated.get("sha") != obj["sha"]:
            break
        obj = annotated.get("object")
    raise NativeReleaseError(
        "The official release tag does not resolve to a verified commit."
    )


def resolve_release(service):
    """Resolve an official published release and capture its immutable artifact IDs."""
    validate_service(service)
    selected = selected_release(service)
    if selected == "latest":
        releases = []
        for page in range(1, 11):
            batch = _json(f"/releases?per_page=100&page={page}")
            if not isinstance(batch, list) or len(batch) > 100:
                raise NativeReleaseError(
                    "Official release discovery returned an invalid list."
                )
            for candidate in batch:
                if (
                    not isinstance(candidate, dict)
                    or candidate.get("draft") is not False
                ):
                    continue
                try:
                    tag = release_tag(candidate.get("tag_name"))
                except NativeReleaseError:
                    continue
                if tag == candidate.get("tag_name"):
                    releases.append(candidate)
            if len(batch) < 100:
                break
        else:
            raise NativeReleaseError(
                "Official v2 release discovery exceeds its supported bound."
            )
        if not releases:
            raise NativeReleaseError(
                "No published official InfiniDysk 2 release was found."
            )
        release = max(releases, key=lambda value: _version_key(value["tag_name"]))
    else:
        release = _json("/releases/tags/" + quote(selected, safe=""))
    if (
        not isinstance(release, dict)
        or release.get("draft") is not False
        or release_tag(release.get("tag_name")) != release.get("tag_name")
        or (selected != "latest" and release["tag_name"] != selected)
    ):
        raise NativeReleaseError(
            "The selected official v2 release is unpublished or inconsistent."
        )
    tag = release["tag_name"]
    arch = architecture()
    name = f"infinidysk-{tag}-linux-{arch}.tar.gz"
    assets = release.get("assets")
    matches = (
        [
            asset
            for asset in assets
            if isinstance(asset, dict) and asset.get("name") == name
        ]
        if isinstance(assets, list)
        else []
    )
    if len(matches) != 1:
        raise NativeReleaseError(
            "The official release does not contain one supported Linux archive."
        )
    asset = matches[0]
    digest = asset.get("digest")
    size = asset.get("size")
    url = f"{DOWNLOAD}/{tag}/{name}"
    if (
        not isinstance(digest, str)
        or not digest.startswith("sha256:")
        or not _SHA.fullmatch(digest[7:])
        or type(size) is not int
        or not 0 < size <= MAX_ARCHIVE
        or asset.get("browser_download_url") != url
    ):
        raise NativeReleaseError(
            "The official archive checksum, size or location is unsupported."
        )
    commit = _tag_commit(tag)
    target = release.get("target_commitish")
    if isinstance(target, str) and _COMMIT.fullmatch(target) and target != commit:
        raise NativeReleaseError("The official release commit and tag disagree.")
    return {
        "release": tag,
        "commit": commit,
        "architecture": arch,
        "asset_name": name,
        "archive_url": url,
        "archive_sha256": digest[7:],
        "archive_size": size,
    }


def _plan(service, plan):
    if not isinstance(plan, dict) or set(plan) != _PLAN_FIELDS:
        raise NativeReleaseError("The native release artifact receipt is invalid.")
    tag = release_tag(plan.get("release"))
    name = f"infinidysk-{tag}-linux-{architecture()}.tar.gz"
    selected = selected_release(service)
    if (
        plan["release"] != tag
        or plan["architecture"] != architecture()
        or (selected != "latest" and selected != tag)
        or plan["asset_name"] != name
        or plan["archive_url"] != f"{DOWNLOAD}/{tag}/{name}"
        or not isinstance(plan["commit"], str)
        or not _COMMIT.fullmatch(plan["commit"])
        or not isinstance(plan["archive_sha256"], str)
        or not _SHA.fullmatch(plan["archive_sha256"])
        or type(plan["archive_size"]) is not int
        or not 0 < plan["archive_size"] <= MAX_ARCHIVE
    ):
        raise NativeReleaseError(
            "The native release artifact receipt does not match its selected official target."
        )


def _download(plan, destination):
    try:
        with (
            urllib.request.urlopen(  # noqa: S310 - canonical verified release URL
                plan["archive_url"], timeout=60
            ) as response,
            open(destination, "xb") as output,
        ):
            final = urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname not in {
                "github.com",
                "release-assets.githubusercontent.com",
                "objects.githubusercontent.com",
            }:
                raise NativeReleaseError(
                    "The official archive redirected to an unsupported origin."
                )
            count = 0
            digest = hashlib.sha256()
            while chunk := response.read(min(1024 * 1024, MAX_ARCHIVE - count + 1)):
                count += len(chunk)
                if count > MAX_ARCHIVE or count > plan["archive_size"]:
                    raise NativeReleaseError(
                        "The official archive exceeds its verified size."
                    )
                digest.update(chunk)
                output.write(chunk)
            if (
                count != plan["archive_size"]
                or digest.hexdigest() != plan["archive_sha256"]
            ):
                raise NativeReleaseError(
                    "The official archive checksum or size did not match."
                )
    except OSError:
        raise NativeReleaseError(
            "The official native archive could not be downloaded."
        ) from None


def _extract(archive, candidate, plan):
    prefix = plan["asset_name"][:-7]
    limits = {
        "infinidysk": MAX_BINARY,
        "LICENSE": 1024 * 1024,
        "version.txt": 256,
        "commit.txt": 256,
    }
    seen = set()
    with tarfile.open(archive, "r:gz") as package:
        for member in package:
            if (
                member.isdir()
                and member.name.rstrip("/") == prefix
                and "directory" not in seen
            ):
                seen.add("directory")
                continue
            relative = PurePosixPath(member.name)
            if (
                relative.name not in limits
                or member.name != f"{prefix}/{relative.name}"
                or not member.isfile()
                or member.issparse()
                or relative.name in seen
                or not 0 <= member.size <= limits[relative.name]
            ):
                raise NativeReleaseError(
                    "The official archive contains an unsupported path, size, link or duplicate."
                )
            seen.add(relative.name)
            with (
                package.extractfile(member) as source,
                open(candidate / relative.name, "xb") as output,
            ):
                shutil.copyfileobj(source, output, length=1024 * 1024)
        if seen - {"directory"} != set(limits):
            raise NativeReleaseError(
                "The official archive is missing required runtime files."
            )
    if (
        release_tag((candidate / "version.txt").read_text().strip()) != plan["release"]
        or (candidate / "commit.txt").read_text().strip() != plan["commit"]
    ):
        raise NativeReleaseError(
            "The official archive version or commit does not match its tag."
        )


@contextmanager
def stage_release(service, plan, *, uid, gid):
    """Yield closed verified runtime files; publication and database safety are external."""
    from utils import infinidysk_v2_runtime as runtime

    validate_service(service)
    _plan(service, plan)
    libc, version = platform.libc_ver()
    if libc != "glibc" or tuple(int(part) for part in version.split(".")[:2]) < (2, 39):
        raise NativeReleaseError("Native InfiniDysk requires glibc 2.39 or newer.")
    if any(type(value) is not int or not 0 < value < 2**31 for value in (uid, gid)):
        raise NativeReleaseError(
            "Native release validation requires non-root runtime ownership."
        )
    root = runtime._directory(runtime.runtime_dir(service))
    root.parent.mkdir(parents=True, exist_ok=True)
    yielded = False
    try:
        with tempfile.TemporaryDirectory(
            prefix=".infinidysk-v2-release-", dir=root.parent
        ) as temporary:
            staging = Path(temporary)
            staging.chmod(0o755)
            archive = staging / "archive.tar.gz"
            _download(plan, archive)
            candidate = staging / "candidate"
            candidate.mkdir(mode=0o755)
            _extract(archive, candidate, plan)
            binary = candidate / "infinidysk"
            binary.chmod(0o755)
            result = subprocess.run(  # noqa: S603 - verified official archive digest/tag/commit
                [str(binary), "openapi"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=True,
                cwd=candidate,
                env={"PATH": os.defpath},
                **(
                    {"user": uid, "group": gid, "extra_groups": []}
                    if os.geteuid() == 0
                    else {}
                ),
            )
            schema = json.loads(result.stdout)
            # /healthz is deliberately not part of the native OpenAPI document;
            # the lifecycle owner checks real readiness after guarded publication.
            required = {"/api/v1/upgrade/status", "/api/v1/catalog"}
            if (
                not isinstance(schema, dict)
                or not isinstance(schema.get("paths"), dict)
                or not required.issubset(schema["paths"])
            ):
                raise NativeReleaseError(
                    "The selected native runtime does not expose DUMB's required v2 API."
                )
            metadata = {
                "format": FORMAT,
                "release": plan["release"],
                "commit": plan["commit"],
                "architecture": plan["architecture"],
                "archive_sha256": plan["archive_sha256"],
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            }
            runtime._private_json(candidate / runtime.MARKER, metadata)
            yielded = True
            yield candidate, metadata
    except NativeReleaseError:
        # These messages are already bounded and credential-free, and identify
        # the specific provenance or compatibility check the user must review.
        raise
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError):
        if yielded:
            raise
        raise NativeReleaseError(
            "The selected native release could not be staged or validated safely."
        ) from None


def _read(path, bound, *, executable=False):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > bound
            or (executable and not os.access(path, os.X_OK))
        ):
            raise NativeReleaseError(
                "Native runtime provenance is not a bounded regular file."
            )
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            value = source.read(bound + 1)
        after = os.fstat(descriptor)
        stamp = lambda row: (
            row.st_dev,
            row.st_ino,
            row.st_size,
            row.st_mtime_ns,
            row.st_ctime_ns,
        )
        if (
            len(value) > bound
            or stamp(before) != stamp(after)
            or stamp(path.lstat()) != stamp(after)
        ):
            raise NativeReleaseError(
                "Native runtime provenance changed while reading it."
            )
        return value
    finally:
        os.close(descriptor)


def installed_release(service):
    """Recognize active official artifacts locally, including legacy pinned markers."""
    from utils import infinidysk_v2_runtime as runtime

    try:
        root = runtime._directory(runtime.runtime_dir(service))
        marker = json.loads(_read(root / runtime.MARKER, 8192))
        if not isinstance(marker, dict):
            return None
        if "format" not in marker:
            # Preserve the established beta6 provenance contract without hashing
            # that same legacy binary twice for every local version observation.
            return runtime.installed_v2_version(service)
        binary = _read(root / "infinidysk", MAX_BINARY, executable=True)
        version = _read(root / "version.txt", 256).decode().strip()
        commit = _read(root / "commit.txt", 256).decode().strip()
        if (
            set(marker) != _MARKER_FIELDS
            or marker.get("format") != FORMAT
            or release_tag(marker.get("release")) != marker.get("release")
            or release_tag(version) != marker["release"]
            or commit != marker.get("commit")
            or not _COMMIT.fullmatch(commit)
            or marker.get("architecture") != architecture()
            or not isinstance(marker.get("archive_sha256"), str)
            or not _SHA.fullmatch(marker["archive_sha256"])
            or marker.get("binary_sha256") != hashlib.sha256(binary).hexdigest()
        ):
            return None
        return marker["release"]
    except (OSError, ValueError, TypeError):
        return None
