import hashlib
import json
import platform
import re
from pathlib import Path
from urllib.parse import urlencode

import requests

OCI_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
OCI_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
OCI_LAYER_MEDIA_TYPES = {
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.docker.image.rootfs.diff.tar.gzip",
}
OCI_ACCEPT = ", ".join(sorted(OCI_INDEX_MEDIA_TYPES | OCI_MANIFEST_MEDIA_TYPES))
_REPOSITORY_PATTERN = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$"
)
_REFERENCE_PATTERN = re.compile(
    r"^(?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|sha256:[a-f0-9]{64})$"
)
_DIGEST_PATTERN = re.compile(r"^sha256:([a-f0-9]{64})$")
_MAX_COMPRESSED_IMAGE_BYTES = 4 * 1024 * 1024 * 1024


class OCIImageError(RuntimeError):
    pass


def normalize_oci_architecture(machine: str | None = None) -> str:
    value = str(machine or platform.machine()).strip().lower()
    if value in {"x86_64", "amd64"}:
        return "amd64"
    if value in {"aarch64", "arm64"}:
        return "arm64"
    raise OCIImageError(f"Unsupported OCI image architecture: {value or 'unknown'}")


def _validate_digest(digest: str) -> str:
    value = str(digest or "").strip().lower()
    if not _DIGEST_PATTERN.fullmatch(value):
        raise OCIImageError("OCI registry returned an invalid sha256 digest.")
    return value


def _content_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _parse_bearer_challenge(header: str) -> dict[str, str]:
    value = str(header or "").strip()
    if not value.lower().startswith("bearer "):
        raise OCIImageError("OCI registry did not provide Bearer authentication.")
    fields = {
        match.group(1).lower(): match.group(2)
        for match in re.finditer(r'(\w+)="([^"]*)"', value[7:])
    }
    if not fields.get("realm"):
        raise OCIImageError("OCI registry authentication challenge is incomplete.")
    return fields


class OCIRegistryClient:
    def __init__(
        self,
        registry: str = "registry-1.docker.io",
        session: requests.Session | None = None,
        timeout: tuple[int, int] = (15, 300),
    ):
        registry_value = str(registry or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9.-]+(?::\d+)?", registry_value):
            raise OCIImageError("Invalid OCI registry hostname.")
        self.registry = registry_value
        self.session = session or requests.Session()
        self.timeout = timeout
        self._tokens: dict[str, str] = {}

    @staticmethod
    def validate_repository(repository: str) -> str:
        value = str(repository or "").strip().lower()
        if not _REPOSITORY_PATTERN.fullmatch(value):
            raise OCIImageError("Invalid OCI image repository name.")
        return value

    @staticmethod
    def validate_reference(reference: str) -> str:
        value = str(reference or "").strip()
        if not _REFERENCE_PATTERN.fullmatch(value):
            raise OCIImageError("Invalid OCI image reference.")
        return value

    def _registry_url(self, repository: str, resource: str) -> str:
        return f"https://{self.registry}/v2/{repository}/{resource}"

    def _request_token(self, challenge: dict[str, str], repository: str) -> str:
        params = {}
        if challenge.get("service"):
            params["service"] = challenge["service"]
        params["scope"] = challenge.get("scope") or f"repository:{repository}:pull"
        separator = "&" if "?" in challenge["realm"] else "?"
        token_url = f"{challenge['realm']}{separator}{urlencode(params)}"
        try:
            response = self.session.get(token_url, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise OCIImageError(
                "Unable to authenticate with the OCI registry."
            ) from exc
        token = payload.get("token") or payload.get("access_token")
        if not token:
            raise OCIImageError("OCI registry authentication returned no token.")
        self._tokens[repository] = str(token)
        return str(token)

    def _get(
        self,
        url: str,
        repository: str,
        *,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> requests.Response:
        request_headers = dict(headers or {})
        token = self._tokens.get(repository)
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        try:
            response = self.session.get(
                url,
                headers=request_headers,
                stream=stream,
                timeout=self.timeout,
                allow_redirects=True,
            )
            if response.status_code == 401:
                challenge = _parse_bearer_challenge(
                    response.headers.get("WWW-Authenticate", "")
                )
                token = self._request_token(challenge, repository)
                request_headers["Authorization"] = f"Bearer {token}"
                response = self.session.get(
                    url,
                    headers=request_headers,
                    stream=stream,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status})" if status else ""
            raise OCIImageError(f"OCI registry request failed{suffix}.") from exc

    def _manifest_response(self, repository: str, reference: str) -> requests.Response:
        return self._get(
            self._registry_url(repository, f"manifests/{reference}"),
            repository,
            headers={"Accept": OCI_ACCEPT},
        )

    def resolve_manifest(
        self,
        repository: str,
        reference: str = "latest",
        architecture: str | None = None,
    ) -> dict:
        repository = self.validate_repository(repository)
        reference = self.validate_reference(reference)
        architecture = normalize_oci_architecture(architecture)
        response = self._manifest_response(repository, reference)
        content = response.content
        if reference.startswith("sha256:") and _content_digest(content) != reference:
            raise OCIImageError("OCI index digest verification failed.")
        try:
            document = json.loads(content)
        except (TypeError, ValueError) as exc:
            raise OCIImageError("OCI registry returned an invalid manifest.") from exc

        media_type = document.get("mediaType") or response.headers.get(
            "Content-Type", ""
        )
        index_digest = response.headers.get("Docker-Content-Digest") or _content_digest(
            content
        )
        index_digest = _validate_digest(index_digest)
        manifest_digest = index_digest

        if media_type in OCI_INDEX_MEDIA_TYPES or "manifests" in document:
            descriptor = next(
                (
                    item
                    for item in document.get("manifests", [])
                    if item.get("platform", {}).get("os") == "linux"
                    and item.get("platform", {}).get("architecture") == architecture
                ),
                None,
            )
            if not descriptor:
                raise OCIImageError(f"OCI image has no linux/{architecture} manifest.")
            manifest_digest = _validate_digest(descriptor.get("digest"))
            response = self._manifest_response(repository, manifest_digest)
            content = response.content
            if _content_digest(content) != manifest_digest:
                raise OCIImageError("OCI platform manifest digest verification failed.")
            try:
                document = json.loads(content)
            except (TypeError, ValueError) as exc:
                raise OCIImageError(
                    "OCI registry returned an invalid manifest."
                ) from exc
            media_type = document.get("mediaType") or response.headers.get(
                "Content-Type", ""
            )

        if media_type not in OCI_MANIFEST_MEDIA_TYPES and "layers" not in document:
            raise OCIImageError("Unsupported OCI manifest media type.")
        layers = document.get("layers")
        if not isinstance(layers, list) or not layers:
            raise OCIImageError("OCI platform manifest contains no layers.")
        compressed_size = 0
        for layer in layers:
            _validate_digest(layer.get("digest"))
            if layer.get("mediaType") not in OCI_LAYER_MEDIA_TYPES:
                raise OCIImageError(
                    f"Unsupported OCI layer media type: {layer.get('mediaType') or 'unknown'}"
                )
            try:
                layer_size = int(layer.get("size", 0))
            except (TypeError, ValueError) as exc:
                raise OCIImageError("OCI image layer has an invalid size.") from exc
            if layer_size < 0:
                raise OCIImageError("OCI image layer has an invalid size.")
            compressed_size += layer_size
            if compressed_size > _MAX_COMPRESSED_IMAGE_BYTES:
                raise OCIImageError("OCI image exceeds the compressed size limit.")
        return {
            "repository": repository,
            "reference": reference,
            "architecture": architecture,
            "index_digest": index_digest,
            "manifest_digest": manifest_digest,
            "layers": layers,
        }

    def download_blob(
        self,
        repository: str,
        descriptor: dict,
        target_path: str | Path,
    ) -> Path:
        repository = self.validate_repository(repository)
        digest = _validate_digest(descriptor.get("digest"))
        expected_size = int(descriptor.get("size", 0) or 0)
        target = Path(target_path)
        hasher = hashlib.sha256()
        bytes_written = 0
        response = self._get(
            self._registry_url(repository, f"blobs/{digest}"),
            repository,
            stream=True,
        )
        try:
            with target.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    hasher.update(chunk)
                    bytes_written += len(chunk)
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise OCIImageError("Unable to write the OCI image layer.") from exc
        finally:
            response.close()
        actual_digest = f"sha256:{hasher.hexdigest()}"
        if actual_digest != digest:
            target.unlink(missing_ok=True)
            raise OCIImageError("OCI image layer digest verification failed.")
        if expected_size and bytes_written != expected_size:
            target.unlink(missing_ok=True)
            raise OCIImageError("OCI image layer size verification failed.")
        return target
