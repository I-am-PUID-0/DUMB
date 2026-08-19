import io
import os
import struct
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.mediastorm_installer import (
    MediaStormInstallError,
    _mediastorm_install_request,
    apply_mediastorm_layer,
    install_mediastorm_runtime,
    mediastorm_app_version_text,
    mediastorm_install_selector,
    mediastorm_runtime_ready,
    mediastorm_runtime_matches_selection,
    mediastorm_target_status,
    normalize_mediastorm_version,
)


def _write_layer(path: Path, entries: dict[str, tuple[bytes, int]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, (content, mode) in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            archive.addfile(info, io.BytesIO(content))


def _write_symlink_layer(path: Path, name: str, target: str) -> None:
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        info.mode = 0o777
        archive.addfile(info)


def _write_mixed_layer(
    path: Path,
    *,
    files: dict[str, tuple[bytes, int]] | None = None,
    symlinks: dict[str, str] | None = None,
    directories: tuple[str, ...] = (),
) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name in directories:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for name, (content, mode) in (files or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            archive.addfile(info, io.BytesIO(content))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            info.mode = 0o777
            archive.addfile(info)


def _minimal_elf_bytes(needed: list[str]) -> bytes:
    """Build a minimal ELF64 dynamic executable with the given DT_NEEDED
    entries, so the installer's runtime library closure sees real deps.

    The fixture is shaped for _elf_dynamic_info(): the zero-sized SHT_DYNSYM
    and the placeholder linked string table are intentional, because the
    parser resolves the dynamic string table through SHT_DYNAMIC.sh_link
    (which points at the same .dynstr section).
    Do not rely on changing e_shstrndx alone; the parser never reads it.
    """
    dynstr = b"\x00" + b"\x00".join(name.encode() for name in needed) + b"\x00"
    str_offsets = {}
    cursor = 1
    for name in needed:
        str_offsets[name] = cursor
        cursor += len(name) + 1
    dynstr_off = 64 + 56
    dyn_entries = [(1, str_offsets[name]) for name in needed]
    dyn_entries.append((5, 0x400000 + dynstr_off))  # DT_STRTAB
    dyn_entries.append((10, len(dynstr)))  # DT_STRSZ
    dyn_entries.append((0, 0))  # DT_NULL
    dynamic_off = dynstr_off + len(dynstr)
    dynamic = b"".join(struct.pack("<qQ", tag, value) for tag, value in dyn_entries)
    shoff = dynamic_off + len(dynamic)
    total = shoff + 5 * 64

    e_ident = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9)
    ehdr = struct.pack(
        "<16sHHIQQQIHHHHHH",
        e_ident,
        2,  # ET_EXEC
        62,  # EM_X86_64
        1,
        0x400000,
        64,
        shoff,
        0,
        64,
        56,
        1,
        64,
        5,
        0,
    )
    phdr = struct.pack(
        "<IIQQQQQQ",
        1,  # PT_LOAD
        5,  # PF_R | PF_X
        0,
        0x400000,
        0x400000,
        total,
        total,
        0x1000,
    )

    def shdr(name, sh_type, offset, size, link=0, addralign=1):
        return struct.pack(
            "<IIQQQQIIQQ",
            name,
            sh_type,
            0,
            0,
            offset,
            size,
            link,
            0,
            addralign,
            0,
        )

    shstr_off = total - 64
    sections = b"".join(
        [
            bytes(64),  # null section
            shdr(0, 3, dynstr_off, len(dynstr), addralign=1),  # .dynstr
            shdr(0, 6, dynamic_off, len(dynamic), link=1, addralign=8),  # .dynamic
            shdr(0, 11, shstr_off, 0, link=1, addralign=8),  # .dynsym
            shdr(0, 3, shstr_off, 1, addralign=1),  # shstrtab placeholder
        ]
    )
    return ehdr + phdr + dynstr + dynamic + sections


class _FakeOCIClient:
    def __init__(self, layers, missing_references=None, index_digest=None):
        self.layers = layers
        self.missing_references = set(missing_references or [])
        self.resolved_references = []
        self.index_digest = index_digest or "sha256:" + "a" * 64

    def resolve_manifest(self, repository, reference):
        self.resolved_references.append(reference)
        if reference in self.missing_references:
            from utils.oci_image import OCIImageError

            raise OCIImageError("OCI registry request failed (HTTP 404).")
        return {
            "repository": repository,
            "reference": reference,
            "architecture": "arm64",
            "index_digest": self.index_digest,
            "manifest_digest": "sha256:" + "b" * 64,
            "layers": [
                {
                    "digest": f"sha256:{index:064x}",
                    "size": source.stat().st_size,
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                }
                for index, source in enumerate(self.layers, start=1)
            ],
        }

    def download_blob(self, repository, descriptor, target_path):
        index = int(descriptor["digest"].split(":", 1)[1], 16) - 1
        Path(target_path).write_bytes(self.layers[index].read_bytes())
        return Path(target_path)


class MediaStormInstallerTests(unittest.TestCase):
    def test_extracts_only_allowlisted_runtime_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layer = root / "layer.tar.gz"
            output = root / "output"
            _write_layer(
                layer,
                {
                    "root/mediastorm": (b"server", 0o755),
                    "opt/strmr-web/index.html": (b"web", 0o644),
                    "etc/shadow": (b"ignored", 0o600),
                },
            )

            apply_mediastorm_layer(layer, output)

            self.assertEqual((output / "mediastorm").read_bytes(), b"server")
            self.assertEqual((output / "web" / "index.html").read_bytes(), b"web")
            self.assertFalse((output / "etc").exists())

    def test_rejects_layer_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layer = root / "layer.tar.gz"
            _write_layer(layer, {"../../opt/strmr-web/escape": (b"bad", 0o644)})

            with self.assertRaises(MediaStormInstallError):
                apply_mediastorm_layer(layer, root / "output")

    def test_extracts_current_app_binary_and_ignores_known_root_alias(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            alias_layer = root / "alias.tar.gz"
            binary_layer = root / "binary.tar.gz"
            output = root / "output"
            _write_symlink_layer(alias_layer, "root/mediastorm", "/app/mediastorm")
            _write_layer(binary_layer, {"app/mediastorm": (b"server", 0o755)})

            extracted_bytes = apply_mediastorm_layer(alias_layer, output)
            apply_mediastorm_layer(binary_layer, output, extracted_bytes)

            self.assertEqual((output / "mediastorm").read_bytes(), b"server")
            self.assertFalse((output / "mediastorm").is_symlink())

    def test_rejects_unexpected_root_binary_link_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layer = root / "layer.tar.gz"
            _write_symlink_layer(layer, "root/mediastorm", "/etc/shadow")

            with self.assertRaisesRegex(
                MediaStormInstallError, "unsupported link: root/mediastorm"
            ):
                apply_mediastorm_layer(layer, root / "output")

    def test_extracts_jellyfin_bundle_as_runtime_relative_links(self):
        # The amd64 images install ffmpeg/ffprobe from the jellyfin-ffmpeg
        # package and expose them through absolute container links under
        # /usr/local/bin. The staged runtime must keep those links relative.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layer = root / "layer.tar.gz"
            output = root / "output"
            _write_mixed_layer(
                layer,
                directories=(
                    "usr/lib/jellyfin-ffmpeg/",
                    "usr/lib/jellyfin-ffmpeg/lib/",
                ),
                files={
                    "usr/lib/jellyfin-ffmpeg/ffmpeg": (b"ffmpeg-binary", 0o755),
                    "usr/lib/jellyfin-ffmpeg/ffprobe": (b"ffprobe-binary", 0o755),
                    "usr/lib/jellyfin-ffmpeg/lib/libz.so.1.3.1": (b"libz", 0o644),
                },
                symlinks={
                    "usr/lib/jellyfin-ffmpeg/lib/libz.so": "libz.so.1.3.1",
                    "usr/local/bin/ffmpeg": "/usr/lib/jellyfin-ffmpeg/ffmpeg",
                    "usr/local/bin/ffprobe": "/usr/lib/jellyfin-ffmpeg/ffprobe",
                },
            )

            apply_mediastorm_layer(layer, output)

            self.assertEqual(
                (output / "lib" / "jellyfin-ffmpeg" / "ffmpeg").read_bytes(),
                b"ffmpeg-binary",
            )
            self.assertTrue((output / "bin" / "ffmpeg").is_symlink())
            self.assertEqual(
                os.readlink(output / "bin" / "ffmpeg"),
                "../lib/jellyfin-ffmpeg/ffmpeg",
            )
            self.assertEqual(
                os.readlink(output / "bin" / "ffprobe"),
                "../lib/jellyfin-ffmpeg/ffprobe",
            )
            self.assertEqual(
                os.readlink(output / "lib" / "jellyfin-ffmpeg" / "lib" / "libz.so"),
                "libz.so.1.3.1",
            )
            self.assertEqual(
                (output / "bin" / "ffmpeg").resolve().read_bytes(),
                b"ffmpeg-binary",
            )

    def test_stages_system_libraries_for_runtime_resolution(self):
        # The jellyfin-ffmpeg layout links ffmpeg/ffprobe against codec and
        # support libraries the image installs as system libraries under
        # /usr/lib/<multiarch>. Every entry there is staged separately so the
        # runtime library closure can carry exactly what is needed; entries
        # outside the multiarch tree stay unmapped.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layer = root / "layer.tar.gz"
            output = root / "output"
            _write_mixed_layer(
                layer,
                directories=("usr/lib/x86_64-linux-gnu/",),
                files={
                    "usr/lib/x86_64-linux-gnu/libmp3lame.so.0.0.0": (
                        b"lame",
                        0o644,
                    ),
                    "usr/lib/x86_64-linux-gnu/libx264.so.164": (b"x264", 0o644),
                    "usr/lib/x86_64-linux-gnu/libvorbis.so.0.4.9": (
                        b"vorbis",
                        0o644,
                    ),
                    "usr/lib/x86_64-linux-gnu/libc.so.6": (b"libc", 0o644),
                    "usr/lib/something-else/libunrelated.so.1": (b"nope", 0o644),
                },
                symlinks={
                    "usr/lib/x86_64-linux-gnu/libmp3lame.so.0": ("libmp3lame.so.0.0.0"),
                    "usr/lib/x86_64-linux-gnu/libvorbis.so.0": ("libvorbis.so.0.4.9"),
                },
            )

            apply_mediastorm_layer(layer, output)

            staging = output / "lib" / ".system-libs"
            self.assertEqual((staging / "libmp3lame.so.0.0.0").read_bytes(), b"lame")
            self.assertTrue((staging / "libmp3lame.so.0").is_symlink())
            self.assertEqual(
                os.readlink(staging / "libmp3lame.so.0"),
                "libmp3lame.so.0.0.0",
            )
            self.assertEqual((staging / "libx264.so.164").read_bytes(), b"x264")
            self.assertEqual(
                os.readlink(staging / "libvorbis.so.0"),
                "libvorbis.so.0.4.9",
            )
            self.assertEqual((staging / "libc.so.6").read_bytes(), b"libc")
            self.assertFalse((staging / "libunrelated.so.1").exists())

    def test_runtime_ready_requires_resolvable_library_closure(self):
        def write_runtime(root, with_codec_lib=False, malformed_ffmpeg=False):
            runtime = root / "runtime"
            for relative in (
                "mediastorm",
                "web/index.html",
                "iroh/iroh-direct-spike",
                "python-venv/bin/python3",
                "bin/yt-dlp",
                "bin/deno",
            ):
                path = runtime / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(relative, encoding="utf-8")
            (runtime / "bin" / "ffprobe").write_bytes(_minimal_elf_bytes([]))
            bundle = runtime / "lib" / "jellyfin-ffmpeg"
            bundle.mkdir(parents=True, exist_ok=True)
            if malformed_ffmpeg:
                # Truncated ELF: the header claims a section table the file
                # does not contain. ready() must reject it without raising.
                elf = _minimal_elf_bytes(["libmediastorm-test-codec.so.0"])
                (bundle / "ffmpeg").write_bytes(elf[: len(elf) // 2])
            else:
                (bundle / "ffmpeg").write_bytes(
                    _minimal_elf_bytes(
                        ["libavcodec.so.61", "libmediastorm-test-codec.so.0"]
                    )
                )
            (runtime / "bin" / "ffmpeg").unlink(missing_ok=True)
            (runtime / "bin" / "ffmpeg").symlink_to("../lib/jellyfin-ffmpeg/ffmpeg")
            (bundle / "lib").mkdir(exist_ok=True)
            (bundle / "lib" / "libavcodec.so.61").write_bytes(_minimal_elf_bytes([]))
            if with_codec_lib:
                (bundle / "lib" / "libmediastorm-test-codec.so.0").write_bytes(
                    _minimal_elf_bytes([])
                )
            return runtime

        # Jellyfin layout without the codec libraries cannot run ffmpeg.
        # Each case gets its own root so the write_runtime invocations are
        # isolated from each other.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertFalse(
                mediastorm_runtime_ready(write_runtime(root, with_codec_lib=False))
            )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertTrue(
                mediastorm_runtime_ready(write_runtime(root, with_codec_lib=True))
            )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            # A truncated ffmpeg ELF must not raise and is not ready.
            self.assertFalse(
                mediastorm_runtime_ready(
                    write_runtime(root, with_codec_lib=True, malformed_ffmpeg=True)
                )
            )

        # Static-binary layouts (arm64 images) never need the bundle libs.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = root / "runtime"
            for relative in (
                "mediastorm",
                "web/index.html",
                "iroh/iroh-direct-spike",
                "python-venv/bin/python3",
                "bin/ffmpeg",
                "bin/ffprobe",
                "bin/yt-dlp",
                "bin/deno",
            ):
                path = runtime / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if relative in ("bin/ffmpeg", "bin/ffprobe"):
                    path.write_bytes(_minimal_elf_bytes([]))
                else:
                    path.write_text(relative, encoding="utf-8")
            self.assertTrue(mediastorm_runtime_ready(runtime))

    def test_install_fails_when_runtime_needs_unknown_system_library(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layer = root / "layer.tar.gz"
            files = {
                "app/mediastorm": (b"server", 0o755),
                "opt/strmr-web/index.html": (b"web", 0o644),
                "opt/iroh/iroh-direct-spike": (b"iroh", 0o755),
                "app/version.txt": (b"1.5.0\n20260811\n", 0o644),
                "parse_title.py": (b"", 0o644),
                "parse_title_batch.py": (b"", 0o644),
                "search_subtitles.py": (b"", 0o644),
                "download_subtitle.py": (b"", 0o644),
                "detect_credits.py": (b"", 0o644),
                "usr/local/bin/ffmpeg": (
                    _minimal_elf_bytes(["libmystery.so.1"]),
                    0o755,
                ),
                "usr/local/bin/ffprobe": (_minimal_elf_bytes([]), 0o755),
                "usr/local/bin/yt-dlp": (b"yt-dlp", 0o755),
                "usr/local/bin/deno": (b"deno", 0o755),
            }
            _write_mixed_layer(layer, files=files)

            def fake_python_environment(runtime):
                python = runtime / "python-venv" / "bin" / "python3"
                python.parent.mkdir(parents=True)
                python.write_text("python", encoding="utf-8")

            config = {
                "config_dir": str(root / "mediastorm"),
            }
            with patch(
                "utils.mediastorm_installer._build_python_environment",
                side_effect=fake_python_environment,
            ):
                with self.assertRaises(MediaStormInstallError) as raised:
                    install_mediastorm_runtime(
                        config,
                        "latest",
                        client=_FakeOCIClient([layer]),
                    )
            self.assertIn("libmystery.so.1", str(raised.exception))

    def test_installs_amd64_runtime_with_jellyfin_ffmpeg_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layer = root / "layer.tar.gz"
            files = {
                "app/mediastorm": (b"server", 0o755),
                "opt/strmr-web/index.html": (b"web", 0o644),
                "opt/iroh/iroh-direct-spike": (b"iroh", 0o755),
                "app/version.txt": (b"1.5.0\n20260811\n", 0o644),
                "parse_title.py": (b"", 0o644),
                "parse_title_batch.py": (b"", 0o644),
                "search_subtitles.py": (b"", 0o644),
                "download_subtitle.py": (b"", 0o644),
                "detect_credits.py": (b"", 0o644),
                "usr/lib/jellyfin-ffmpeg/ffmpeg": (
                    _minimal_elf_bytes(["libmp3lame.so.0", "libx264.so.164"]),
                    0o755,
                ),
                "usr/lib/jellyfin-ffmpeg/ffprobe": (_minimal_elf_bytes([]), 0o755),
                "usr/local/bin/yt-dlp": (b"yt-dlp", 0o755),
                "usr/local/bin/deno": (b"deno", 0o755),
                "usr/lib/x86_64-linux-gnu/libmp3lame.so.0.0.0": (
                    _minimal_elf_bytes(["libvorbis.so.0"]),
                    0o644,
                ),
                "usr/lib/x86_64-linux-gnu/libx264.so.164": (b"x264", 0o644),
                "usr/lib/x86_64-linux-gnu/libvorbis.so.0.4.9": (
                    _minimal_elf_bytes([]),
                    0o644,
                ),
            }
            _write_mixed_layer(
                layer,
                files=files,
                symlinks={
                    "root/mediastorm": "/app/mediastorm",
                    "usr/local/bin/ffmpeg": "/usr/lib/jellyfin-ffmpeg/ffmpeg",
                    "usr/local/bin/ffprobe": "/usr/lib/jellyfin-ffmpeg/ffprobe",
                    "usr/lib/x86_64-linux-gnu/libmp3lame.so.0": ("libmp3lame.so.0.0.0"),
                    "usr/lib/x86_64-linux-gnu/libvorbis.so.0": ("libvorbis.so.0.4.9"),
                },
            )

            def fake_python_environment(runtime):
                python = runtime / "python-venv" / "bin" / "python3"
                python.parent.mkdir(parents=True)
                python.write_text("python", encoding="utf-8")

            config = {
                "config_dir": str(root / "mediastorm"),
            }
            with patch(
                "utils.mediastorm_installer._build_python_environment",
                side_effect=fake_python_environment,
            ):
                result = install_mediastorm_runtime(
                    config,
                    "latest",
                    client=_FakeOCIClient([layer]),
                )

            runtime = root / "mediastorm" / "runtime"
            self.assertEqual(result["version"], "v1.5.0-20260811")
            self.assertTrue(mediastorm_runtime_ready(runtime))
            self.assertEqual(
                os.readlink(runtime / "bin" / "ffmpeg"),
                "../lib/jellyfin-ffmpeg/ffmpeg",
            )
            self.assertEqual(
                (runtime / "bin" / "ffmpeg").resolve().read_bytes()[:4],
                b"\x7fELF",
            )
            self.assertEqual(
                (runtime / "lib" / "jellyfin-ffmpeg" / "ffprobe").read_bytes()[:4],
                b"\x7fELF",
            )
            # The codec libraries ffmpeg links against were carried into the
            # bundle directory and the staging area was pruned.
            bundle_lib = runtime / "lib" / "jellyfin-ffmpeg" / "lib"
            self.assertEqual(
                (bundle_lib / "libmp3lame.so.0").read_bytes(),
                _minimal_elf_bytes(["libvorbis.so.0"]),
            )
            self.assertEqual(
                (bundle_lib / "libvorbis.so.0").read_bytes(),
                _minimal_elf_bytes([]),
            )
            self.assertEqual((bundle_lib / "libx264.so.164").read_bytes(), b"x264")
            self.assertFalse((runtime / "lib" / ".system-libs").exists())

    def test_installs_verified_runtime_atomically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layer = root / "layer.tar.gz"
            entries = {
                "root/mediastorm": (b"server", 0o755),
                "opt/strmr-web/index.html": (b"web", 0o644),
                "opt/iroh/iroh-direct-spike": (b"iroh", 0o755),
                "app/version.txt": (b"1.5.0\n20260711\n", 0o644),
                "parse_title.py": (b"", 0o644),
                "parse_title_batch.py": (b"", 0o644),
                "search_subtitles.py": (b"", 0o644),
                "download_subtitle.py": (b"", 0o644),
                "detect_credits.py": (b"", 0o644),
            }
            for binary in ("ffmpeg", "ffprobe"):
                entries[f"usr/local/bin/{binary}"] = (_minimal_elf_bytes([]), 0o755)
            for binary in ("yt-dlp", "deno"):
                entries[f"usr/local/bin/{binary}"] = (b"binary", 0o755)
            _write_layer(layer, entries)

            def fake_python_environment(runtime):
                python = runtime / "python-venv" / "bin" / "python3"
                python.parent.mkdir(parents=True)
                python.write_text("python", encoding="utf-8")

            config = {
                "config_dir": str(root / "mediastorm"),
            }
            with patch(
                "utils.mediastorm_installer._build_python_environment",
                side_effect=fake_python_environment,
            ):
                result = install_mediastorm_runtime(
                    config,
                    "v1.5.0-20260711",
                    client=_FakeOCIClient([layer]),
                )

            runtime = root / "mediastorm" / "runtime"
            self.assertTrue(mediastorm_runtime_ready(runtime))
            self.assertEqual(result["version"], "v1.5.0-20260711")
            self.assertEqual(result["oci_reference"], "latest")
            self.assertEqual(result["install_selector"], "latest")
            self.assertEqual(
                (runtime / "version.txt").read_text(encoding="utf-8").strip(),
                "v1.5.0-20260711",
            )
            self.assertEqual(
                (runtime / "app-version.txt").read_text(encoding="utf-8"),
                "1.5.0\n20260711\n",
            )
            self.assertEqual(
                (runtime / "scripts" / "search_subtitles.py").stat().st_mode & 0o777,
                0o644,
            )
            self.assertEqual(
                (runtime / "install-selector.txt").read_text(encoding="utf-8").strip(),
                "latest",
            )
            self.assertTrue(mediastorm_runtime_matches_selection(runtime, "latest"))

    def test_latest_uses_oci_version_when_github_release_metadata_lags(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layer = root / "layer.tar.gz"
            entries = {
                "root/mediastorm": (b"server", 0o755),
                "opt/strmr-web/index.html": (b"web", 0o644),
                "opt/iroh/iroh-direct-spike": (b"iroh", 0o755),
                "app/version.txt": (b"1.5.0\n20260807\n", 0o644),
            }
            for binary in ("ffmpeg", "ffprobe"):
                entries[f"usr/local/bin/{binary}"] = (_minimal_elf_bytes([]), 0o755)
            for binary in ("yt-dlp", "deno"):
                entries[f"usr/local/bin/{binary}"] = (b"binary", 0o755)
            _write_layer(layer, entries)

            def fake_python_environment(runtime):
                python = runtime / "python-venv" / "bin" / "python3"
                python.parent.mkdir(parents=True)
                python.write_text("python", encoding="utf-8")

            config = {"config_dir": str(root / "mediastorm")}
            with patch(
                "utils.mediastorm_installer._build_python_environment",
                side_effect=fake_python_environment,
            ):
                result = install_mediastorm_runtime(
                    config,
                    "v1.5.0-20260806",
                    client=_FakeOCIClient([layer]),
                )

            self.assertEqual(result["version"], "v1.5.0-20260807")
            self.assertEqual(result["install_selector"], "latest")

    def test_target_status_compares_selected_oci_digest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / "mediastorm" / "runtime"
            for relative in (
                "mediastorm",
                "web/index.html",
                "iroh/iroh-direct-spike",
                "python-venv/bin/python3",
                "bin/ffmpeg",
                "bin/ffprobe",
                "bin/yt-dlp",
                "bin/deno",
            ):
                target = runtime / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if relative in ("bin/ffmpeg", "bin/ffprobe"):
                    target.write_bytes(_minimal_elf_bytes([]))
                else:
                    target.write_text("runtime", encoding="utf-8")
            (runtime / "version.txt").write_text("v1.5.0-20260806\n", encoding="utf-8")
            (runtime / "install-selector.txt").write_text("latest\n", encoding="utf-8")
            old_digest = "sha256:" + "c" * 64
            current_digest = "sha256:" + "a" * 64
            (runtime / "image-digest.txt").write_text(
                f"{old_digest}\n", encoding="utf-8"
            )
            config = {"config_dir": str(runtime.parent)}
            client = _FakeOCIClient([], index_digest=current_digest)

            pending = mediastorm_target_status(config, client=client)
            self.assertFalse(pending["installed"])
            self.assertEqual(pending["current_digest"], old_digest)
            self.assertEqual(pending["available_digest"], current_digest)

            (runtime / "image-digest.txt").write_text(
                f"{current_digest}\n", encoding="utf-8"
            )
            installed = mediastorm_target_status(config, client=client)
            self.assertTrue(installed["installed"])

    def test_installs_pinned_semver_reference_and_accepts_dated_internal_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layer = root / "layer.tar.gz"
            entries = {
                "root/mediastorm": (b"server", 0o755),
                "opt/strmr-web/index.html": (b"web", 0o644),
                "opt/iroh/iroh-direct-spike": (b"iroh", 0o755),
                "app/version.txt": (b"1.5.0\n20260711\n", 0o644),
            }
            for binary in ("ffmpeg", "ffprobe"):
                entries[f"usr/local/bin/{binary}"] = (_minimal_elf_bytes([]), 0o755)
            for binary in ("yt-dlp", "deno"):
                entries[f"usr/local/bin/{binary}"] = (b"binary", 0o755)
            _write_layer(layer, entries)

            def fake_python_environment(runtime):
                python = runtime / "python-venv" / "bin" / "python3"
                python.parent.mkdir(parents=True)
                python.write_text("python", encoding="utf-8")

            client = _FakeOCIClient([layer])
            config = {
                "config_dir": str(root / "mediastorm"),
                "release_version_enabled": True,
                "release_version": "1.5.0",
            }
            with patch(
                "utils.mediastorm_installer._build_python_environment",
                side_effect=fake_python_environment,
            ):
                result = install_mediastorm_runtime(config, "1.5.0", client=client)

            runtime = root / "mediastorm" / "runtime"
            self.assertEqual(client.resolved_references, ["1.5.0"])
            self.assertEqual(result["version"], "v1.5.0-20260711")
            self.assertEqual(result["oci_reference"], "1.5.0")
            self.assertTrue(mediastorm_runtime_matches_selection(runtime, "1.5.0"))
            self.assertFalse(mediastorm_runtime_matches_selection(runtime, "latest"))

    def test_github_release_pin_falls_back_to_semver_oci_tag(self):
        config = {
            "release_version_enabled": True,
            "release_version": "v1.5.0-20260711",
        }
        request = _mediastorm_install_request(config, "v1.5.0-20260711")

        self.assertEqual(request["references"], ["1.5.020260711", "1.5.0"])
        self.assertEqual(request["expected_version"], "v1.5.0-20260711")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layer = root / "layer.tar.gz"
            entries = {
                "root/mediastorm": (b"server", 0o755),
                "opt/strmr-web/index.html": (b"web", 0o644),
                "opt/iroh/iroh-direct-spike": (b"iroh", 0o755),
                "app/version.txt": (b"1.5.0\n20260711\n", 0o644),
            }
            for binary in ("ffmpeg", "ffprobe"):
                entries[f"usr/local/bin/{binary}"] = (_minimal_elf_bytes([]), 0o755)
            for binary in ("yt-dlp", "deno"):
                entries[f"usr/local/bin/{binary}"] = (b"binary", 0o755)
            _write_layer(layer, entries)

            def fake_python_environment(runtime):
                python = runtime / "python-venv" / "bin" / "python3"
                python.parent.mkdir(parents=True)
                python.write_text("python", encoding="utf-8")

            config["config_dir"] = str(root / "mediastorm")
            commit_sha = "c" * 40
            client = _FakeOCIClient(
                [layer], missing_references={"1.5.020260711", commit_sha}
            )
            with patch(
                "utils.mediastorm_installer._build_python_environment",
                side_effect=fake_python_environment,
            ):
                result = install_mediastorm_runtime(
                    config,
                    "v1.5.0-20260711",
                    client=client,
                    release_ref_resolver=lambda _tag: (commit_sha, None),
                )

            self.assertEqual(
                client.resolved_references,
                ["1.5.020260711", commit_sha, "1.5.0"],
            )
            self.assertEqual(result["oci_reference"], "1.5.0")
            self.assertEqual(result["version"], "v1.5.0-20260711")

    def test_github_release_pin_prefers_immutable_commit_oci_tag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layer = root / "layer.tar.gz"
            entries = {
                "root/mediastorm": (b"server", 0o755),
                "opt/strmr-web/index.html": (b"web", 0o644),
                "opt/iroh/iroh-direct-spike": (b"iroh", 0o755),
                "app/version.txt": (b"1.5.0\n20260806\n", 0o644),
            }
            for binary in ("ffmpeg", "ffprobe"):
                entries[f"usr/local/bin/{binary}"] = (_minimal_elf_bytes([]), 0o755)
            for binary in ("yt-dlp", "deno"):
                entries[f"usr/local/bin/{binary}"] = (b"binary", 0o755)
            _write_layer(layer, entries)

            def fake_python_environment(runtime):
                python = runtime / "python-venv" / "bin" / "python3"
                python.parent.mkdir(parents=True)
                python.write_text("python", encoding="utf-8")

            commit_sha = "c" * 40
            config = {
                "config_dir": str(root / "mediastorm"),
                "release_version_enabled": True,
                "release_version": "v1.5.0-20260806",
            }
            client = _FakeOCIClient([layer], missing_references={"1.5.020260806"})
            with patch(
                "utils.mediastorm_installer._build_python_environment",
                side_effect=fake_python_environment,
            ):
                result = install_mediastorm_runtime(
                    config,
                    "v1.5.0-20260806",
                    client=client,
                    release_ref_resolver=lambda tag: (
                        (commit_sha, None)
                        if tag == "v1.5.0-20260806"
                        else (None, "unexpected tag")
                    ),
                )

            self.assertEqual(
                client.resolved_references,
                ["1.5.020260806", commit_sha],
            )
            self.assertEqual(result["oci_reference"], commit_sha)
            self.assertEqual(result["version"], "v1.5.0-20260806")

    def test_accepts_commit_and_digest_pins_but_rejects_arbitrary_tags(self):
        commit = "a" * 40
        digest = "sha256:" + "b" * 64
        self.assertEqual(
            mediastorm_install_selector(
                {
                    "release_version_enabled": True,
                    "release_version": commit,
                }
            ),
            commit,
        )
        self.assertEqual(
            mediastorm_install_selector(
                {
                    "release_version_enabled": True,
                    "release_version": digest,
                }
            ),
            digest,
        )
        with self.assertRaises(MediaStormInstallError):
            install_mediastorm_runtime(
                {
                    "config_dir": "/tmp/unused-mediastorm-test",
                    "release_version_enabled": True,
                    "release_version": "debug",
                },
                "debug",
                client=_FakeOCIClient([]),
            )

    def test_version_mismatch_preserves_existing_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "mediastorm"
            runtime = config_dir / "runtime"
            runtime.mkdir(parents=True)
            (runtime / "sentinel").write_text("keep", encoding="utf-8")
            layer = root / "layer.tar.gz"
            _write_layer(layer, {"app/version.txt": (b"9.9.9\n", 0o644)})

            with self.assertRaises(MediaStormInstallError):
                install_mediastorm_runtime(
                    {
                        "config_dir": str(config_dir),
                        "container_image": "godver3/mediastorm",
                        "release_version_enabled": True,
                        "release_version": "v1.5.0-20260711",
                    },
                    "v1.5.0-20260711",
                    client=_FakeOCIClient([layer]),
                )

            self.assertEqual((runtime / "sentinel").read_text(encoding="utf-8"), "keep")

    def test_normalizes_upstream_two_line_version(self):
        self.assertEqual(
            normalize_mediastorm_version("1.5.0\n20260711\n"),
            "v1.5.0-20260711",
        )

    def test_restores_mediastorm_two_line_version_file(self):
        self.assertEqual(
            mediastorm_app_version_text("v1.5.0-20260711\n"),
            "1.5.0\n20260711\n",
        )


if __name__ == "__main__":
    unittest.main()
