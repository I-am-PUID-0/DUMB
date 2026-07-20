import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from utils.oci_image import OCIImageError, OCIRegistryClient, normalize_oci_architecture


class _Response:
    def __init__(self, status, content, headers):
        self.status_code = status
        self.content = content
        self.headers = dict(headers or {})
        self.raw = io.BytesIO(content)
        self.url = "https://registry.example.invalid/test"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self.content)

    def iter_content(self, chunk_size):
        while True:
            chunk = self.raw.read(chunk_size)
            if not chunk:
                return
            yield chunk

    def close(self):
        self.raw.close()


def _response(status, content=b"", headers=None):
    return _Response(status, content, headers)


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)

    def get(self, *args, **kwargs):
        return self.responses.pop(0)


class OCIImageTests(unittest.TestCase):
    def test_normalizes_supported_architectures(self):
        self.assertEqual(normalize_oci_architecture("x86_64"), "amd64")
        self.assertEqual(normalize_oci_architecture("aarch64"), "arm64")
        with self.assertRaises(OCIImageError):
            normalize_oci_architecture("riscv64")

    def test_selects_and_verifies_platform_manifest(self):
        manifest = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": "sha256:" + "c" * 64,
                    "size": 123,
                }
            ],
        }
        manifest_bytes = json.dumps(manifest).encode()
        manifest_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        index = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "digest": manifest_digest,
                    "platform": {"os": "linux", "architecture": "arm64"},
                }
            ],
        }
        index_bytes = json.dumps(index).encode()
        index_digest = "sha256:" + hashlib.sha256(index_bytes).hexdigest()
        session = _Session(
            [
                _response(
                    200,
                    index_bytes,
                    {"Docker-Content-Digest": index_digest},
                ),
                _response(200, manifest_bytes),
            ]
        )

        resolved = OCIRegistryClient(
            registry="registry.example.invalid", session=session
        ).resolve_manifest("example/mediastorm", "latest", "arm64")

        self.assertEqual(resolved["manifest_digest"], manifest_digest)
        self.assertEqual(resolved["layers"], manifest["layers"])

    def test_download_blob_rejects_digest_mismatch(self):
        content = b"not-the-expected-content"
        client = OCIRegistryClient(
            registry="registry.example.invalid",
            session=_Session([_response(200, content)]),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "blob"
            with self.assertRaises(OCIImageError):
                client.download_blob(
                    "example/mediastorm",
                    {"digest": "sha256:" + "a" * 64, "size": len(content)},
                    target,
                )
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
