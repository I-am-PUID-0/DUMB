import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils.aiostreams_installer import (
    AIOStreamsInstallError,
    aiostreams_install_selector,
    aiostreams_runtime_ready,
    aiostreams_target_status,
    apply_aiostreams_layer,
    install_aiostreams_runtime,
)


def _write_layer(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755 if name.endswith(".so.2") else 0o644
            archive.addfile(info, io.BytesIO(content))


def _runtime_files(version: str = "2.33.2") -> dict[str, bytes]:
    return {
        "app/package.json": json.dumps(
            {"name": "aiostreams", "version": version}
        ).encode(),
        "app/LICENSE": b"GPL-3.0 license text",
        "app/packages/server/package.json": b'{"name":"@aiostreams/server"}',
        "app/packages/server/dist/server.js": b"console.log('server');",
        "app/packages/server/node_modules/better-sqlite3/package.json": b"{}",
        "app/packages/core/package.json": b'{"name":"@aiostreams/core"}',
        "app/packages/core/dist/index.js": b"export {};",
        "app/packages/core/node_modules/zod/package.json": b"{}",
        "app/packages/frontend/dist/index.html": b"<html></html>",
        "app/node_modules/.pnpm/lock.yaml": b"lockfileVersion: '9.0'",
        "app/resources/metadata.json": b'{"channel":"stable"}',
        "usr/local/lib/libmimalloc.so.2": b"mimalloc",
    }


class _FakeOCIClient:
    def __init__(self, layers, digest=None, missing_references=None):
        self.layers = list(layers)
        self.digest = digest or "sha256:" + "a" * 64
        self.missing_references = set(missing_references or ())
        self.references = []

    def resolve_manifest(self, repository, reference):
        self.references.append(reference)
        if reference in self.missing_references:
            from utils.oci_image import OCIImageError

            raise OCIImageError("not found")
        return {
            "repository": repository,
            "reference": reference,
            "architecture": "amd64",
            "index_digest": self.digest,
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
        del repository
        index = int(descriptor["digest"].split(":", 1)[1], 16) - 1
        Path(target_path).write_bytes(self.layers[index].read_bytes())
        return Path(target_path)


class AIOStreamsInstallerTests(unittest.TestCase):
    def test_selector_accepts_only_latest_or_stable_release(self):
        self.assertEqual("latest", aiostreams_install_selector({}))
        self.assertEqual(
            "v2.33.2",
            aiostreams_install_selector(
                {"release_version_enabled": True, "release_version": "v2.33.2"}
            ),
        )
        with self.assertRaises(AIOStreamsInstallError):
            aiostreams_install_selector(
                {"release_version_enabled": True, "release_version": "main"}
            )

    def test_extracts_only_app_and_runtime_scoped_mimalloc(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layer = root / "layer.tar.gz"
            output = root / "output"
            _write_layer(
                layer,
                {
                    "app/package.json": b"{}",
                    "usr/local/lib/libmimalloc.so.2": b"allocator",
                    "etc/shadow": b"ignored",
                },
            )

            apply_aiostreams_layer(layer, output)

            self.assertEqual((output / "package.json").read_bytes(), b"{}")
            self.assertEqual(
                (output / "lib" / "libmimalloc.so.2").read_bytes(), b"allocator"
            )
            self.assertFalse((output / "etc").exists())

    def test_rejects_path_and_link_escape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            traversal = root / "traversal.tar.gz"
            _write_layer(traversal, {"../../app/escape": b"bad"})
            with self.assertRaises(AIOStreamsInstallError):
                apply_aiostreams_layer(traversal, root / "traversal-output")

            link = root / "link.tar.gz"
            with tarfile.open(link, "w:gz") as archive:
                info = tarfile.TarInfo("app/node_modules/escape")
                info.type = tarfile.SYMTYPE
                info.linkname = "../../../etc/shadow"
                archive.addfile(info)
            with self.assertRaises(AIOStreamsInstallError):
                apply_aiostreams_layer(link, root / "link-output")

    def test_runtime_requires_built_dependencies_license_and_allocator(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layer = root / "layer.tar.gz"
            runtime = root / "runtime"
            _write_layer(layer, _runtime_files())
            apply_aiostreams_layer(layer, runtime)
            self.assertTrue(aiostreams_runtime_ready(runtime))

            (runtime / "packages" / "server" / "node_modules").rename(
                runtime / "packages" / "server" / "missing-node-modules"
            )
            self.assertFalse(aiostreams_runtime_ready(runtime))

    def test_native_sqlite_probe_resolves_from_core_workspace(self):
        from utils.aiostreams_installer import _validate_node_runtime

        completed = Mock(returncode=0, stdout="v24.19.0\n", stderr="")
        with (
            patch("utils.aiostreams_installer.shutil.which", return_value="/node"),
            patch(
                "utils.aiostreams_installer.subprocess.run",
                return_value=completed,
            ) as run,
        ):
            _validate_node_runtime(Path("/runtime"))

        self.assertEqual(run.call_count, 3)
        native_command = run.call_args_list[2].args[0]
        self.assertEqual(native_command[0:2], ["/node", "-e"])
        self.assertIn("/packages/core/package.json", native_command[2])
        self.assertNotIn("/packages/server/package.json", native_command[2])
        self.assertEqual(native_command[3], "/runtime")

    def test_install_replaces_only_runtime_and_records_verified_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "aiostreams"
            (config_dir / "runtime").mkdir(parents=True)
            (config_dir / "runtime" / "old.txt").write_text("old")
            (config_dir / "data").mkdir()
            (config_dir / "data" / "db.sqlite").write_text("persistent")
            layer = root / "layer.tar.gz"
            _write_layer(layer, _runtime_files())
            client = _FakeOCIClient([layer])

            with patch(
                "utils.aiostreams_installer._validate_node_runtime"
            ) as validate_node:
                result = install_aiostreams_runtime(
                    {"config_dir": str(config_dir)}, client=client
                )

            validate_node.assert_called_once()
            self.assertEqual("2.33.2", result["version"])
            self.assertFalse((config_dir / "runtime" / "old.txt").exists())
            self.assertEqual(
                "persistent", (config_dir / "data" / "db.sqlite").read_text()
            )
            self.assertEqual(
                "latest",
                (config_dir / "runtime" / "install-selector.txt").read_text().strip(),
            )
            self.assertEqual(
                client.digest,
                (config_dir / "runtime" / "image-digest.txt").read_text().strip(),
            )

            status = aiostreams_target_status(
                {"config_dir": str(config_dir)}, client=client
            )
            self.assertTrue(status["installed"])

    def test_fixed_release_rejects_mismatched_image_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            layer = root / "layer.tar.gz"
            _write_layer(layer, _runtime_files("2.33.1"))
            client = _FakeOCIClient([layer], missing_references={"v2.33.2"})
            config = {
                "config_dir": str(root / "aiostreams"),
                "release_version_enabled": True,
                "release_version": "v2.33.2",
            }

            with (
                patch("utils.aiostreams_installer._validate_node_runtime"),
                self.assertRaisesRegex(AIOStreamsInstallError, "version mismatch"),
            ):
                install_aiostreams_runtime(config, client=client)

            self.assertEqual(["v2.33.2", "2.33.2"], client.references)


if __name__ == "__main__":
    unittest.main()
