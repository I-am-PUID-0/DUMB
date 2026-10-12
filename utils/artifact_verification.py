"""Verify managed bootstrap artifacts using reviewed, bundled publisher keys."""

import base64
import binascii
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


class ArtifactVerificationError(ValueError):
    """An artifact cannot be authenticated as its expected publisher."""


_KEY_DIRECTORY = Path(__file__).resolve().parent / "signing_keys"
_PUBLISHERS = {
    "bun": (
        _KEY_DIRECTORY / "bun.asc",
        "F3DCC08A8572C0749B3E18888EAB4D40A7B22B59",
    ),
    "dotnet": (
        _KEY_DIRECTORY / "dotnet.asc",
        "2B930AB1228D11D5D7F6B6ACB9CF1A51FC7D3ACF",
    ),
}
_REJECTED_STATUSES = {
    "BADSIG",
    "ERRSIG",
    "NO_PUBKEY",
    "EXPSIG",
    "EXPKEYSIG",
    "REVKEYSIG",
    "FAILURE",
    "ERROR",
}


def _decode_key(armored: bytes) -> bytes:
    """Decode a bundled public key without requiring the full GnuPG toolset."""
    match = re.fullmatch(
        rb"-----BEGIN PGP PUBLIC KEY BLOCK-----\r?\n"
        rb"(?:[^\r\n]+\r?\n)*?\r?\n"
        rb"([A-Za-z0-9+/=\r\n]+)"
        rb"-----END PGP PUBLIC KEY BLOCK-----\s*",
        armored,
    )
    if not match:
        raise ArtifactVerificationError("Bundled publisher key has invalid armor")
    lines = match.group(1).splitlines()
    # The armor CRC is transport metadata; gpgv authenticates the signing key
    # against the pinned full fingerprint after reading the decoded packets.
    if lines and lines[-1].startswith(b"="):
        lines.pop()
    try:
        return base64.b64decode(b"".join(lines), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ArtifactVerificationError("Bundled publisher key is invalid") from exc


def verify_publisher_signature(artifact: Path, signature: Path, publisher: str) -> None:
    """Authenticate a detached signature or an identical clear-signed body.

    No user keyrings, downloaded keys, or key servers participate in trust.
    Successful verification requires the pinned primary fingerprint and a
    SHA-256, SHA-384, or SHA-512 signature digest.
    """
    if publisher not in _PUBLISHERS:
        raise ArtifactVerificationError("Unknown artifact publisher")
    verifier = shutil.which("gpgv")
    if not verifier:
        raise ArtifactVerificationError("gpgv is required for publisher verification")
    key_path, fingerprint = _PUBLISHERS[publisher]
    try:
        key = _decode_key(key_path.read_bytes())
        clear_signed = signature.read_bytes().startswith(
            b"-----BEGIN PGP SIGNED MESSAGE-----"
        )
        temporary_root = Path("/tmp")
        temporary_stat = temporary_root.lstat()
        if (
            not stat.S_ISDIR(temporary_stat.st_mode)
            or temporary_stat.st_uid not in {0, os.geteuid()}
            or (
                temporary_stat.st_mode & 0o022
                and not temporary_stat.st_mode & stat.S_ISVTX
            )
        ):
            raise ArtifactVerificationError("Signature staging root is not protected")
        with tempfile.TemporaryDirectory(
            prefix="dumb-signature-", dir=temporary_root
        ) as directory:
            root = Path(directory)
            keyring = root / "publisher.gpg"
            keyring.write_bytes(key)
            verified_body = root / "verified.txt"
            command = [
                verifier,
                "--homedir",
                directory,
                "--keyring",
                str(keyring),
                "--status-fd",
                "1",
            ]
            if clear_signed:
                command.extend(["--output", str(verified_body), str(signature)])
            else:
                command.extend([str(signature), str(artifact)])
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=30, check=False
            )
            valid = False
            rejected = False
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) < 2 or fields[0] != "[GNUPG:]":
                    continue
                status = fields[1]
                if status in _REJECTED_STATUSES:
                    rejected = True
                if status == "VALIDSIG" and len(fields) in (11, 12):
                    # VALIDSIG: signer, date, timestamp, expiration, version,
                    # reserved, pubkey algorithm, hash algorithm, signature
                    # class, and optional primary fingerprint for subkeys.
                    primary = fields[11] if len(fields) == 12 else fields[2]
                    if primary == fingerprint and fields[9] in {"8", "9", "10"}:
                        valid = True
                    else:
                        rejected = True
            if result.returncode != 0 or rejected or not valid:
                raise ArtifactVerificationError(
                    f"Publisher signature verification failed for {publisher}"
                )
            if clear_signed:

                def normalized(body: bytes) -> bytes:
                    return body.replace(b"\r\n", b"\n").rstrip(b"\n")

                if normalized(verified_body.read_bytes()) != normalized(
                    artifact.read_bytes()
                ):
                    raise ArtifactVerificationError(
                        "Signed manifest does not match downloaded manifest"
                    )
    except ArtifactVerificationError:
        raise
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise ArtifactVerificationError(
            f"Unable to verify {publisher} publisher signature"
        ) from exc
