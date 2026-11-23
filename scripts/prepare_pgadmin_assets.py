"""Build missing pgAdmin web assets from authenticated, same-version source."""

import ast
import importlib.metadata
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler

from utils.artifact_verification import verify_publisher_signature

ASSET_DIRECTORY = "pgadmin/static/js/generated"
REQUIRED_ASSETS = (
    "style.css",
    "vendor.react.js",
    "vendor.main.js",
    "vendor.others.js",
    "vendor.sqleditor.js",
    "pgadmin_commons.js",
    "security.pages.js",
    "app.bundle.js",
    "browser_nodes.js",
    "sqleditor.js",
)


class PgAdminAssetError(ValueError):
    """The selected pgAdmin runtime cannot be completed safely."""


def missing_assets(package_root):
    root = Path(package_root) / ASSET_DIRECTORY
    return [
        name
        for name in REQUIRED_ASSETS
        if not (root / name).is_file() or not (root / name).stat().st_size
    ]


def version_number(path):
    """Read upstream's version integer without executing package code."""
    for node in ast.parse(Path(path).read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "APP_VERSION_INT"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    raise PgAdminAssetError("pgAdmin source has no explicit application version.")


def _validate_url(value):
    address = urlsplit(value)
    if (
        address.scheme != "https"
        or address.hostname != "ftp.postgresql.org"
        or address.username
        or address.password
        or address.port not in (None, 443)
        or address.query
        or address.fragment
    ):
        raise PgAdminAssetError("Unexpected pgAdmin source download URL.")


class _SourceRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        _validate_url(new_url)
        return super().redirect_request(request, fp, code, message, headers, new_url)


def download_source(version, directory):
    base = f"https://ftp.postgresql.org/pub/pgadmin/pgadmin4/v{version}/source/"
    paths = []
    for suffix, maximum in ((".tar.gz", 128 * 1024**2), (".tar.gz.asc", 64 * 1024)):
        name = f"pgadmin4-{version}{suffix}"
        url = base + name
        _validate_url(url)
        destination = Path(directory) / name
        with (
            build_opener(_SourceRedirectHandler()).open(url, timeout=30) as response,
            destination.open("wb") as target,
        ):
            _validate_url(response.geturl())
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > maximum:
                    raise PgAdminAssetError(
                        "pgAdmin source download exceeds its limit."
                    )
                target.write(chunk)
        paths.append(destination)
    return tuple(paths)


def extract_source(archive, directory, version):
    prefix = f"pgadmin4-{version}/"
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if (
            len(members) > 100000
            or sum(member.size for member in members) > 1024**3
            or any(
                ".." in PurePosixPath(member.name).parts
                or "\\" in member.name
                or (
                    member.name.rstrip("/") != prefix.rstrip("/")
                    and not member.name.startswith(prefix)
                )
                for member in members
            )
        ):
            raise PgAdminAssetError("pgAdmin source archive layout is invalid.")
        source.extractall(directory, members=members, filter="data")
    return Path(directory) / f"pgadmin4-{version}" / "web"


def prepare_assets(package_root, version, *, fetch=download_source, run=subprocess.run):
    package_root = Path(package_root)
    if not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", version):
        raise PgAdminAssetError("pgAdmin asset builds require an exact stable version.")
    parts = [int(part) for part in version.split(".")]
    expected = parts[0] * 10000 + parts[1] * 100 + (parts[2] if len(parts) > 2 else 0)
    if version_number(package_root / "version.py") != expected:
        raise PgAdminAssetError("The installed pgAdmin package version does not match.")
    if not missing_assets(package_root):
        return False
    target = package_root / ASSET_DIRECTORY
    if target.is_symlink() or not target.parent.resolve().is_relative_to(
        package_root.resolve()
    ):
        raise PgAdminAssetError("The pgAdmin generated asset directory is a symlink.")
    with tempfile.TemporaryDirectory(prefix="dumb-pgadmin-assets-") as temporary:
        archive, signature = fetch(version, Path(temporary))
        verify_publisher_signature(Path(archive), Path(signature), "pgadmin")
        source = extract_source(archive, temporary, version)
        if version_number(source / "version.py") != expected:
            raise PgAdminAssetError("The signed pgAdmin source version does not match.")
        environment = os.environ.copy()
        environment.update(
            NODE_ENV="production", NODE_OPTIONS="--max-old-space-size=3072"
        )
        for command in (
            ["corepack", "yarn", "install", "--immutable"],
            ["corepack", "yarn", "run", "bundle"],
        ):
            run(command, cwd=source, env=environment, check=True)
        if missing_assets(source):
            raise PgAdminAssetError("The pgAdmin web build did not produce its assets.")
        generated = source / ASSET_DIRECTORY
        if generated.is_symlink() or any(
            path.is_symlink() for path in generated.rglob("*")
        ):
            raise PgAdminAssetError("The pgAdmin web build contains linked assets.")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(generated, target)
    if missing_assets(package_root):
        raise PgAdminAssetError("Installed pgAdmin web assets are incomplete.")
    return True


def main():
    try:
        distribution = importlib.metadata.distribution("pgadmin4")
        rebuilt = prepare_assets(
            distribution.locate_file("pgadmin4"), distribution.version
        )
        print(
            f"pgAdmin {distribution.version} web assets "
            f"{'rebuilt from signed matching source' if rebuilt else 'verified'}."
        )
        return 0
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        tarfile.TarError,
        importlib.metadata.PackageNotFoundError,
    ) as error:
        print(f"pgAdmin web asset preparation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
