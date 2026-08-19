import os
import posixpath
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable

from utils.download import Downloader
from utils.global_logger import logger
from utils.oci_image import OCIImageError, OCIRegistryClient

MEDIASTORM_PYTHON_PACKAGES = ("parsett==1.8.5", "subliminal==2.6.0")
MEDIASTORM_OCI_REGISTRY = "registry-1.docker.io"
MEDIASTORM_OCI_REPOSITORY = "godver3/mediastorm"
MEDIASTORM_OCI_REFERENCE = "latest"
MEDIASTORM_SOURCE_OWNER = "godver3"
MEDIASTORM_SOURCE_REPOSITORY = "mediastorm"
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
_SOURCE_DIRECTORIES = {
    "opt/strmr-web": "web",
    "opt/iroh": "iroh",
    # The amd64 images install ffmpeg/ffprobe from the jellyfin-ffmpeg package
    # and expose them through absolute container links under /usr/local/bin.
    # Extract the whole bundle so the binaries and their bundled libraries
    # travel with the staged runtime.
    "usr/lib/jellyfin-ffmpeg": "lib/jellyfin-ffmpeg",
}

# The jellyfin-ffmpeg binaries also link against codec and support libraries
# that the image installs as system libraries under /usr/lib/<multiarch>
# (mp3lame, opus, vorbis, theora, x264/x265, bluray, OpenCL, and their
# transitive dependencies). Those packages are not guaranteed on the DUMB
# host, so every entry under that multiarch tree is staged into
# lib/.system-libs during layer application. Once all layers are applied,
# _resolve_runtime_libraries() reads the DT_NEEDED graph of the extracted
# ffmpeg/ffprobe and carries exactly the missing libraries into the bundle
# directory where LD_LIBRARY_PATH already points, then discards the staging
# area. This keeps the runtime self-contained and stays correct when
# upstream images bump SONAMEs or change the codec set.
_SYSTEM_LIB_STAGING_DIR = "lib/.system-libs"

# Libraries every glibc-based DUMB host guarantees; the runtime never needs
# to carry them. If a future image requires anything else from the host,
# the install fails loudly listing the missing libraries.
_CORE_HOST_LIBRARIES = frozenset(
    {
        "libc.so.6",
        "libm.so.6",
        "libpthread.so.0",
        "libdl.so.2",
        "librt.so.1",
        "ld-linux-x86-64.so.2",
        "ld-linux-aarch64.so.1",
        "libgcc_s.so.1",
        "libstdc++.so.6",
    }
)


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
    parent, _, name = source_path.rpartition("/")
    if parent.startswith("usr/lib/") and parent.endswith("-linux-gnu"):
        # System libraries of the image's base distribution are staged
        # separately and pruned after the runtime library closure resolves
        # which ones ffmpeg/ffprobe actually need.
        return f"{_SYSTEM_LIB_STAGING_DIR}/{name}"
    return None


def _mapped_link_target(source_path: str, linkname: str) -> str | None:
    """Resolve a container link to its allowlisted runtime path, if any.

    Absolute link targets resolve from the container root; relative targets
    resolve from the link's own directory. Only targets that stay inside an
    allowlisted source path are considered supported.
    """
    target = str(linkname or "").replace("\\", "/")
    if not target:
        return None
    if target.startswith("/"):
        resolved = posixpath.normpath(target[1:])
    else:
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(source_path), target)
        )
    if not resolved or ".." in PurePosixPath(resolved).parts:
        return None
    return _mapped_path(resolved)


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
                mapped_target = (
                    _mapped_link_target(source_path, member.linkname)
                    if member.issym()
                    else None
                )
                if mapped_target is None:
                    raise MediaStormInstallError(
                        f"mediastorm OCI runtime contains an unsupported link: {source_path}"
                    )
                if mapped_target == mapped:
                    # The link is a container-internal alias for the same
                    # runtime file (for example /root/mediastorm ->
                    # /app/mediastorm). The regular file is extracted from its
                    # own layer entry, so do not reproduce the alias.
                    continue
                # Rewrite container links as runtime-relative symlinks so the
                # staged runtime stays self-contained (for example
                # /usr/local/bin/ffmpeg -> /usr/lib/jellyfin-ffmpeg/ffmpeg).
                relative_target = posixpath.relpath(
                    mapped_target, posixpath.dirname(mapped)
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    _remove_existing(destination)
                try:
                    destination.symlink_to(relative_target)
                except OSError as exc:
                    raise MediaStormInstallError(
                        f"Unable to install mediastorm OCI link: {source_path}"
                    ) from exc
                continue
            if member.isdir():
                if destination.exists() and not destination.is_dir():
                    _remove_existing(destination)
                destination.mkdir(parents=True, exist_ok=True)
                destination.chmod(member.mode & 0o777)
                continue
            if not member.isfile():
                raise MediaStormInstallError(
                    f"mediastorm OCI runtime contains an unsupported entry: {source_path}"
                )
            extracted_bytes += int(member.size or 0)
            if extracted_bytes > _MAX_EXTRACTED_BYTES:
                raise MediaStormInstallError(
                    "mediastorm OCI runtime exceeds the size limit."
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                _remove_existing(destination)
            source = archive.extractfile(member)
            if source is None:
                raise MediaStormInstallError(
                    f"Unable to read mediastorm OCI entry: {source_path}"
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
    """Convert DUMB's normalized marker back to mediastorm's file format."""
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
            "mediastorm release pinning is enabled but release_version is empty."
        )
    if selector.lower() == "latest":
        return MEDIASTORM_OCI_REFERENCE
    return selector


def _mediastorm_install_request(config: dict, requested_version: str) -> dict:
    selector = mediastorm_install_selector(config)
    if selector == MEDIASTORM_OCI_REFERENCE:
        return {
            "selector": selector,
            "references": [MEDIASTORM_OCI_REFERENCE],
            # The OCI latest tag can move before the matching GitHub release
            # is published. Its digest and embedded version marker are the
            # authoritative update identity for this moving channel.
            "expected_version": None,
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
        "Invalid mediastorm release_version. Use latest, a release tag "
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


# Bounds for the ELF section header table parse: real files carry only a
# handful of sections, so cap the count and the table size before reading
# malformed or hostile inputs.
_MAX_ELF_SECTIONS = 4096
_MAX_ELF_SECTION_TABLE_BYTES = 1 << 20
# Individual reads of the dynamic section and its string table are capped
# too (real files carry only a few KB there), so corrupt section sizes can
# never trigger excessive allocation.
_MAX_ELF_DYNAMIC_READ_BYTES = 1 << 20


def _elf_header_layout(path: Path) -> tuple[bool, str, int, int, int] | None:
    """Validate an ELF header and return its section table layout.

    Returns (is64, endian, shoff, shentsize, shnum) for structurally valid
    ELF files, or None for non-ELF, truncated or malformed input. A missing
    section table (e_shoff == 0 with e_shnum == 0) is accepted as valid for
    static binaries; ELF header validation stays independent of program- and
    section-header presence. When a table is present, the section entry size
    must cover the sh_link read (offset 40 in the 64-bit layout; 32-bit files
    keep their standard 40-byte minimum), and the section count and table
    size are bounded before any allocation.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(64)
            if len(header) < 64 or header[:4] != b"\x7fELF":
                return None
            is64 = header[4] == 2
            endian = "<" if header[5] == 1 else ">"
            if is64:
                shoff = struct.unpack_from(endian + "Q", header, 40)[0]
                shentsize = struct.unpack_from(endian + "H", header, 58)[0]
                shnum = struct.unpack_from(endian + "H", header, 60)[0]
            else:
                shoff = struct.unpack_from(endian + "I", header, 32)[0]
                shentsize = struct.unpack_from(endian + "H", header, 46)[0]
                shnum = struct.unpack_from(endian + "H", header, 48)[0]
    except OSError:
        return None
    if shoff == 0 and shnum == 0:
        # No section header table at all: valid for static binaries. The
        # dynamic-metadata callers treat this as "no sections" and report
        # no DT_NEEDED entries, so such files stay usable.
        return is64, endian, shoff, shentsize, shnum
    if (
        shnum == 0
        or shnum > _MAX_ELF_SECTIONS
        or shentsize < (44 if is64 else 40)
        or shentsize * shnum > _MAX_ELF_SECTION_TABLE_BYTES
    ):
        return None
    return is64, endian, shoff, shentsize, shnum


def _elf_dynamic_info(path: Path) -> tuple[bytes | None, list[tuple[int, int]]]:
    """Return (strtab bytes, dynamic entries) of an ELF file.

    Only the ELF header, section header table, dynamic section and its string
    table are read (offset/size validated against the file length and capped),
    so probing large libraries is cheap. Non-ELF or malformed input yields
    (None, []).
    """
    layout = _elf_header_layout(path)
    if layout is None:
        return None, []
    is64, endian, shoff, shentsize, shnum = layout
    entry_size = 16 if is64 else 8
    try:
        with path.open("rb") as handle:
            handle.seek(shoff)
            table = handle.read(shentsize * shnum)
    except OSError:
        return None, []
    sections = []
    for index in range(shnum):
        offset = index * shentsize
        block = table[offset : offset + shentsize]
        if len(block) < shentsize:
            return None, []
        sh_type = struct.unpack_from(endian + "I", block, 4)[0]
        if is64:
            sh_offset = struct.unpack_from(endian + "Q", block, 24)[0]
            sh_size = struct.unpack_from(endian + "Q", block, 32)[0]
            sh_link = struct.unpack_from(endian + "I", block, 40)[0]
        else:
            sh_offset = struct.unpack_from(endian + "I", block, 16)[0]
            sh_size = struct.unpack_from(endian + "I", block, 20)[0]
            sh_link = struct.unpack_from(endian + "I", block, 24)[0]
        sections.append((sh_type, sh_offset, sh_size, sh_link))

    # DT_NEEDED strings resolve against the string table the SHT_DYNAMIC
    # section links to (sh_link), so derive strtab from that link instead
    # of SHT_DYNSYM's.
    dynamic_section = None
    dynamic_sh_link = None
    for sh_type, sh_offset, sh_size, sh_link in sections:
        if sh_type == 6:  # SHT_DYNAMIC: sh_link points at its string table
            dynamic_section = (sh_offset, sh_size)
            dynamic_sh_link = sh_link
    if dynamic_section is None:
        return None, []
    if dynamic_sh_link >= len(sections):
        return None, []
    strtab_section = sections[dynamic_sh_link][1:3]
    strtab_offset, strtab_size = strtab_section
    dynamic_offset, dynamic_size = dynamic_section
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            file_size = handle.tell()
            # Validate every offset/size against the real file length and
            # cap the reads, so corrupt section headers cannot force huge
            # allocations; invalid or oversized sections simply yield no
            # dynamic metadata.
            if not (
                strtab_offset + strtab_size <= file_size
                and strtab_size <= _MAX_ELF_DYNAMIC_READ_BYTES
                and dynamic_offset + dynamic_size <= file_size
                and dynamic_size <= _MAX_ELF_DYNAMIC_READ_BYTES
            ):
                return None, []
            handle.seek(strtab_offset)
            strtab = handle.read(strtab_size)
            handle.seek(dynamic_offset)
            dynamic_data = handle.read(dynamic_size)
    except OSError:
        return None, []
    entries = []
    for offset in range(0, len(dynamic_data) - entry_size + 1, entry_size):
        d_tag, d_val = struct.unpack_from(
            endian + ("qQ" if is64 else "iI"), dynamic_data, offset
        )
        entries.append((d_tag, d_val))
        if d_tag == 0:
            break
    return strtab, entries


def _elf_is_parseable(path: Path) -> bool:
    """True when the file is a structurally complete ELF.

    mediastorm_runtime_ready() uses this to reject runtimes whose
    ffmpeg/ffprobe binaries are truncated or corrupt, while static binaries
    (valid ELF files without a dynamic section) still pass.
    """
    layout = _elf_header_layout(path)
    if layout is None:
        return False
    _is64, _endian, shoff, shentsize, shnum = layout
    try:
        with path.open("rb") as handle:
            handle.seek(shoff)
            table = handle.read(shentsize * shnum)
    except OSError:
        return False
    return len(table) >= shentsize * shnum


def _elf_string(entries: list[tuple[int, int]], strtab: bytes, tag: int) -> list[str]:
    values = []
    for d_tag, d_val in entries:
        if d_tag == tag:
            end = strtab.find(b"\x00", d_val)
            if d_val < len(strtab) and end != -1:
                values.append(strtab[d_val:end].decode("utf-8", "replace"))
        elif d_tag == 0:
            break
    return values


def _elf_dynamic_metadata(path: Path) -> tuple[list[str], str | None]:
    """Return (DT_NEEDED libraries, DT_SONAME) of an ELF file.

    Both values come from a single _elf_dynamic_info() parse, so callers
    that need the dependency closure and the soname (bundle indexing,
    closure walking) never parse the same file twice. Files without
    dynamic information yield ([], None).
    """
    strtab, entries = _elf_dynamic_info(path)
    if strtab is None:
        return [], None
    needed = _elf_string(entries, strtab, 1)  # DT_NEEDED
    sonames = _elf_string(entries, strtab, 14)  # DT_SONAME
    return needed, (sonames[0] if sonames else None)


def _is_leaf_library_name(name: str) -> bool:
    """True when the name is a plain leaf library name.

    Absolute paths, path components and traversal names must never be used
    to construct paths into the staged library areas, so dependency names
    parsed from ELF metadata pass this guard before any filesystem access.
    """
    return (
        bool(name) and "/" not in name and "\\" not in name and name not in (".", "..")
    )


def _walk_runtime_closure(runtime: Path, *, copy_missing: bool) -> list[str]:
    """Walk the DT_NEEDED graph of bin/ffmpeg and bin/ffprobe.

    Libraries are resolved against the staged bundle directory, the staged
    system-library area and the core libraries every glibc host provides;
    the sonames that remain unresolved are returned. When copy_missing is
    set, each unresolved library found in the staging area is first copied
    into the bundle directory (where LD_LIBRARY_PATH points); otherwise
    the walk stays read-only. Staging-directory cleanup is left to the
    caller.
    """
    bundle_dir = runtime / "lib" / "jellyfin-ffmpeg" / "lib"
    staging_dir = runtime / _SYSTEM_LIB_STAGING_DIR
    dynamic_cache: dict[Path, tuple[list[str], str | None]] = {}

    def dynamic_metadata(path: Path) -> tuple[list[str], str | None]:
        resolved = path.resolve()
        if resolved not in dynamic_cache:
            dynamic_cache[resolved] = _elf_dynamic_metadata(resolved)
        return dynamic_cache[resolved]

    # Index the bundle by the soname each file declares and by its file
    # name, so a dependency spelled either way (DT_NEEDED normally uses the
    # SONAME, but libraries without one are referenced by filename) resolves
    # to the same candidate. Symlinks are indexed by name only.
    by_soname: dict[str, Path] = {}
    if bundle_dir.is_dir():
        for candidate in bundle_dir.iterdir():
            if candidate.is_file():
                if candidate.is_symlink():
                    by_soname.setdefault(candidate.name, candidate)
                else:
                    _, soname = dynamic_metadata(candidate)
                    by_soname.setdefault(candidate.name, candidate)
                    if soname:
                        by_soname.setdefault(soname, candidate)
    queue = []
    for seed in (runtime / "bin" / "ffmpeg", runtime / "bin" / "ffprobe"):
        if seed.is_file():
            queue.extend(dynamic_metadata(seed)[0])
    missing = []
    seen = set()
    while queue:
        soname = queue.pop()
        if soname in seen:
            continue
        seen.add(soname)
        if not _is_leaf_library_name(soname):
            missing.append(soname)
            continue
        if soname in by_soname:
            queue.extend(dynamic_metadata(by_soname[soname])[0])
            continue
        if soname in _CORE_HOST_LIBRARIES:
            continue
        staged = staging_dir / soname
        if staging_dir.is_dir() and (staged.is_file() or staged.is_symlink()):
            resolved = staged.resolve()
            if resolved.is_file():
                if copy_missing:
                    destination = bundle_dir / soname
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists() or destination.is_symlink():
                        _remove_existing(destination)
                    shutil.copy2(resolved, destination)
                queue.extend(dynamic_metadata(resolved)[0])
                continue
        missing.append(soname)
    return missing


def _runtime_missing_libraries(runtime: Path) -> list[str]:
    """Libraries the staged runtime links against that it cannot provide.

    Walks the DT_NEEDED graph of bin/ffmpeg and bin/ffprobe over the staged
    bundle directory, the staged system-library area and the core libraries
    every glibc host provides. Returns the sonames that remain unresolved.
    """
    return _walk_runtime_closure(runtime, copy_missing=False)


def _resolve_runtime_libraries(runtime: Path) -> list[str]:
    """Carry the system libraries ffmpeg/ffprobe need into the bundle dir.

    Copies each unresolved library from the staged system-library area into
    lib/jellyfin-ffmpeg/lib (where LD_LIBRARY_PATH points), then discards the
    staging area. Returns the sonames that could not be resolved; callers
    should treat any entry as a hard install failure.
    """
    missing = _walk_runtime_closure(runtime, copy_missing=True)
    staging_dir = runtime / _SYSTEM_LIB_STAGING_DIR
    if staging_dir.is_dir():
        _remove_existing(staging_dir)
    return missing


# The sonames the host dynamic loader can satisfy (from `ldconfig -p`),
# probed once on first use. None means "not probed yet".
_HOST_LOADER_LIBRARIES: frozenset[str] | None = None


def _host_loader_provides(soname: str) -> bool:
    """True when the host dynamic loader can satisfy the soname.

    Consults `ldconfig -p` (the loader cache the dynamic linker uses) once
    and remembers the result. Hosts without ldconfig, or where the query
    fails, are treated as providing nothing, so an unknown environment
    never masks a missing library.
    """
    global _HOST_LOADER_LIBRARIES
    if _HOST_LOADER_LIBRARIES is None:
        names = []
        try:
            result = subprocess.run(
                ["ldconfig", "-p"], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        else:
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if " => " in line:
                        names.append(line.split()[0])
        _HOST_LOADER_LIBRARIES = frozenset(names)
    return soname in _HOST_LOADER_LIBRARIES


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
    if not all(path.is_file() for path in required):
        return False
    # A truncated or corrupt ffmpeg/ffprobe cannot be validated and would
    # not run; treat the runtime as not ready instead of assuming an
    # empty dependency closure.
    for seed in (runtime / "bin" / "ffmpeg", runtime / "bin" / "ffprobe"):
        if not _elf_is_parseable(seed):
            logger.warning("mediastorm runtime binary is not a parseable ELF: %s", seed)
            return False
    # The runtime is only usable when every library ffmpeg/ffprobe link
    # against resolves inside the staged runtime, is a core glibc library,
    # or is provided by the host loader. Runtimes staged before the codec
    # libraries were carried fail this check and are reinstalled by the
    # update manager.
    missing = _runtime_missing_libraries(runtime)
    unresolved = [name for name in missing if not _host_loader_provides(name)]
    if unresolved:
        logger.warning(
            "mediastorm runtime is missing libraries the host loader "
            "cannot provide: %s",
            ", ".join(sorted(unresolved)),
        )
        return False
    return True


def mediastorm_target_status(
    config: dict,
    *,
    client: OCIRegistryClient | None = None,
) -> dict:
    """Compare the installed runtime with the selected OCI manifest digest."""
    selector = mediastorm_install_selector(config)
    runtime = Path(config.get("config_dir") or "/mediastorm") / "runtime"
    registry_client = client or OCIRegistryClient(registry=MEDIASTORM_OCI_REGISTRY)
    try:
        manifest = registry_client.resolve_manifest(MEDIASTORM_OCI_REPOSITORY, selector)
    except OCIImageError as exc:
        raise MediaStormInstallError(
            "Unable to resolve the selected mediastorm OCI image."
        ) from exc

    def read_marker(name: str) -> str:
        try:
            return (runtime / name).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    current_version = normalize_mediastorm_version(read_marker("version.txt"))
    current_digest = read_marker("image-digest.txt").lower()
    available_digest = str(manifest["index_digest"]).strip().lower()
    selector_matches = mediastorm_runtime_matches_selection(runtime, selector)
    installed = bool(
        mediastorm_runtime_ready(runtime)
        and selector_matches
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


def _build_python_environment(runtime_dir: Path) -> None:
    python_executable = shutil.which("python3.11")
    if not python_executable:
        raise MediaStormInstallError("Python 3.11 is required to install mediastorm.")
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
                f"Failed to prepare mediastorm Python environment: {tail}"
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
    release_ref_resolver: Callable[[str], tuple[str | None, str | None]] | None = None,
) -> dict:
    config_dir = Path(config.get("config_dir") or "/mediastorm")
    runtime_dir = config_dir / "runtime"
    repository = MEDIASTORM_OCI_REPOSITORY
    install_request = _mediastorm_install_request(config, requested_version)
    config_dir.mkdir(parents=True, exist_ok=True)
    registry_client = client or OCIRegistryClient(registry=MEDIASTORM_OCI_REGISTRY)

    manifest = None
    resolve_errors = []
    references = list(install_request["references"])
    for index, reference in enumerate(references):
        try:
            manifest = registry_client.resolve_manifest(repository, reference)
            break
        except OCIImageError as exc:
            resolve_errors.append(f"{reference}: {exc}")
            if index == 0 and install_request["expected_version"]:
                resolver = release_ref_resolver
                if resolver is None:
                    downloader = Downloader()
                    resolver = lambda release_tag: downloader.get_ref_commit_sha(
                        MEDIASTORM_SOURCE_OWNER,
                        MEDIASTORM_SOURCE_REPOSITORY,
                        release_tag,
                    )
                release_tag = install_request["expected_version"]
                commit_sha, commit_error = resolver(release_tag)
                if commit_sha and _MEDIASTORM_COMMIT_PATTERN.fullmatch(commit_sha):
                    references.insert(index + 1, commit_sha.lower())
                elif commit_error:
                    logger.warning(
                        "Could not resolve mediastorm release %s to its immutable "
                        "OCI commit tag: %s",
                        release_tag,
                        commit_error,
                    )
    if manifest is None:
        raise MediaStormInstallError(
            "Unable to resolve the requested mediastorm OCI reference: "
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
                "Downloading mediastorm OCI layer %d/%d (%.1f MiB).",
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

        missing_libraries = _resolve_runtime_libraries(staged_runtime)
        if missing_libraries:
            raise MediaStormInstallError(
                "Downloaded mediastorm OCI runtime links against system "
                "libraries that are neither bundled nor available on this "
                "host: " + ", ".join(sorted(set(missing_libraries)))
            )

        upstream_version_path = staged_runtime / "app-version.txt"
        try:
            actual_version = normalize_mediastorm_version(
                upstream_version_path.read_text(encoding="utf-8")
            )
        except OSError as exc:
            raise MediaStormInstallError(
                "mediastorm OCI image contains no version marker."
            ) from exc
        if not _MEDIASTORM_RELEASE_PATTERN.fullmatch(actual_version.removeprefix("v")):
            raise MediaStormInstallError(
                "mediastorm OCI image contains an invalid version marker."
            )
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
                "mediastorm OCI version mismatch: "
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
                "Downloaded mediastorm OCI runtime is incomplete."
            )
        _atomic_replace_runtime(staged_runtime, runtime_dir)

    return {
        "version": actual_version,
        "image_digest": manifest["index_digest"],
        "oci_reference": manifest["reference"],
        "install_selector": install_request["selector"],
        "runtime_dir": str(runtime_dir),
    }
