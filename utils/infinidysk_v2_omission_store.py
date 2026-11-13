"""Descriptor-bound private omission sidecars; never embed full proofs in jobs."""

from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat

from utils.infinidysk_v2_omissions import (
    MAX_OMISSION_PROOF_BYTES,
    OmissionProofError,
    validate_omission_proof,
)
from utils.private_files import atomic_write_private_text

_FILENAME = "omission-proof.json"
_JOB = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ERROR = "The retained private omission proof could not be verified safely; run a fresh rehearsal."


def _canonical(value):
    result = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()
    if len(result) > MAX_OMISSION_PROOF_BYTES:
        raise OmissionProofError(
            "The complete private omission proof envelope exceeds its storage bound."
        )
    return result


def _binding(job_id, plan_fingerprint):
    if (
        type(job_id) is not str
        or not _JOB.fullmatch(job_id)
        or type(plan_fingerprint) is not str
        or not _DIGEST.fullmatch(plan_fingerprint)
    ):
        raise OmissionProofError(_ERROR)


def _identity(value):
    return value.st_dev, value.st_ino


def _stamp(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
    )


@contextmanager
def _root(root):
    path = Path(root)
    if (
        not path.is_absolute()
        or path == Path("/")
        or ".." in path.parts
        or len(path.parts) > 128
        or len(os.fsencode(path)) > 16384
    ):
        raise OmissionProofError(_ERROR)
    descriptors, links = [], []
    try:
        descriptor = os.open(
            "/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        descriptors.append(descriptor)
        for part in path.parts[1:]:
            parent = descriptor
            descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
            descriptors.append(descriptor)
            links.append((parent, part, descriptor))

        def check():
            if _identity(os.stat("/")) != _identity(os.fstat(descriptors[0])):
                raise OmissionProofError(_ERROR)
            for parent, name, child in links:
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(current.st_mode) or _identity(current) != _identity(
                    os.fstat(child)
                ):
                    raise OmissionProofError(_ERROR)
            value = os.fstat(descriptor)
            if value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) != 0o700:
                raise OmissionProofError(_ERROR)

        check()
        yield descriptor, check
        check()
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read(descriptor):
    try:
        leaf = os.open(
            _FILENAME,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=descriptor,
        )
    except FileNotFoundError:
        return None
    with os.fdopen(leaf, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size > MAX_OMISSION_PROOF_BYTES
        ):
            raise OmissionProofError(_ERROR)
        raw = handle.read(MAX_OMISSION_PROOF_BYTES + 1)
        after = os.fstat(handle.fileno())
        named = os.stat(_FILENAME, dir_fd=descriptor, follow_symlinks=False)
        if (
            len(raw) != before.st_size
            or len(raw) > MAX_OMISSION_PROOF_BYTES
            or _stamp(before) != _stamp(after)
            or _stamp(before) != _stamp(named)
        ):
            raise OmissionProofError(_ERROR)
    return raw, before


def _reference(raw, value):
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "identity": list(_identity(value)),
    }


def _decode(raw, job_id, plan_fingerprint):
    envelope = json.loads(raw)
    if (
        type(envelope) is not dict
        or set(envelope) != {"job_id", "plan_fingerprint", "proof"}
        or envelope["job_id"] != job_id
        or envelope["plan_fingerprint"] != plan_fingerprint
        or _canonical(envelope) != raw
    ):
        raise OmissionProofError(_ERROR)
    return validate_omission_proof(envelope["proof"])


def write_proof(root, job_id, plan_fingerprint, proof):
    """Atomically retain one complete proof and return a small identity-bound ref."""
    try:
        _binding(job_id, plan_fingerprint)
        validate_omission_proof(proof)
        raw = _canonical(
            {"job_id": job_id, "plan_fingerprint": plan_fingerprint, "proof": proof}
        )
        with _root(root) as (descriptor, check):
            previous = _read(descriptor)
            if previous is not None:
                if not hmac.compare_digest(previous[0], raw):
                    raise OmissionProofError(
                        "A different private omission proof already exists; retain its evidence and rehearse again."
                    )
                _decode(previous[0], job_id, plan_fingerprint)
                check()
                return _reference(*previous)
            check()
            # Anchor the existing atomic writer to the verified directory FD;
            # pathname swaps cannot redirect proof bytes outside the owned root.
            target = Path("/proc/self/fd") / str(descriptor) / _FILENAME
            atomic_write_private_text(
                target, raw.decode("ascii"), owner=(os.geteuid(), os.getegid())
            )
            check()
            result = _read(descriptor)
            if result is None or not hmac.compare_digest(result[0], raw):
                raise OmissionProofError(_ERROR)
            _decode(result[0], job_id, plan_fingerprint)
            return _reference(*result)
    except OmissionProofError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise OmissionProofError(_ERROR) from None


def read_proof(root, job_id, plan_fingerprint, ref):
    """Read only the fixed, bounded private file matching its retained reference."""
    try:
        _binding(job_id, plan_fingerprint)
        if (
            type(ref) is not dict
            or set(ref) != {"sha256", "size", "identity"}
            or type(ref["sha256"]) is not str
            or not _DIGEST.fullmatch(ref["sha256"])
            or type(ref["size"]) is not int
            or not 0 < ref["size"] <= MAX_OMISSION_PROOF_BYTES
            or type(ref["identity"]) is not list
            or len(ref["identity"]) != 2
            or any(type(value) is not int or value < 0 for value in ref["identity"])
        ):
            raise OmissionProofError(_ERROR)
        with _root(root) as (descriptor, check):
            result = _read(descriptor)
            if result is None or _reference(*result) != ref:
                raise OmissionProofError(_ERROR)
            proof = _decode(result[0], job_id, plan_fingerprint)
            check()
            return proof
    except OmissionProofError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise OmissionProofError(_ERROR) from None
