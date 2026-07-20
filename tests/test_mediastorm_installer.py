import io
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
    mediastorm_install_selector,
    mediastorm_runtime_ready,
    mediastorm_runtime_matches_selection,
    normalize_mediastorm_version,
)


def _write_layer(path: Path, entries: dict[str, tuple[bytes, int]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, (content, mode) in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = mode
            archive.addfile(info, io.BytesIO(content))


class _FakeOCIClient:
    def __init__(self, layers, missing_references=None):
        self.layers = layers
        self.missing_references = set(missing_references or [])
        self.resolved_references = []

    def resolve_manifest(self, repository, reference):
        self.resolved_references.append(reference)
        if reference in self.missing_references:
            from utils.oci_image import OCIImageError

            raise OCIImageError("OCI registry request failed (HTTP 404).")
        return {
            "repository": repository,
            "reference": reference,
            "architecture": "arm64",
            "index_digest": "sha256:" + "a" * 64,
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
            for binary in ("ffmpeg", "ffprobe", "yt-dlp", "deno"):
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
                (runtime / "scripts" / "search_subtitles.py").stat().st_mode & 0o777,
                0o644,
            )
            self.assertEqual(
                (runtime / "install-selector.txt").read_text(encoding="utf-8").strip(),
                "latest",
            )
            self.assertTrue(mediastorm_runtime_matches_selection(runtime, "latest"))

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
            for binary in ("ffmpeg", "ffprobe", "yt-dlp", "deno"):
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
            for binary in ("ffmpeg", "ffprobe", "yt-dlp", "deno"):
                entries[f"usr/local/bin/{binary}"] = (b"binary", 0o755)
            _write_layer(layer, entries)

            def fake_python_environment(runtime):
                python = runtime / "python-venv" / "bin" / "python3"
                python.parent.mkdir(parents=True)
                python.write_text("python", encoding="utf-8")

            config["config_dir"] = str(root / "mediastorm")
            client = _FakeOCIClient([layer], missing_references={"1.5.020260711"})
            with patch(
                "utils.mediastorm_installer._build_python_environment",
                side_effect=fake_python_environment,
            ):
                result = install_mediastorm_runtime(
                    config, "v1.5.0-20260711", client=client
                )

            self.assertEqual(client.resolved_references, ["1.5.020260711", "1.5.0"])
            self.assertEqual(result["oci_reference"], "1.5.0")
            self.assertEqual(result["version"], "v1.5.0-20260711")

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


if __name__ == "__main__":
    unittest.main()
