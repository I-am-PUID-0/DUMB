"""Complete private consent evidence for pinned native media omissions.

Public report samples are never a consent ledger. These proofs stay in the
controller-owned job workspace and are additionally bound to its reviewed plan
by the migration manager; no request may supply replacement omission records.
"""

import hashlib
import hmac
import json
import re

PROOF_POLICY = "infinidysk-beta6-reviewed-omissions-v2"
MAX_OMISSION_PROOF_BYTES = 32_000_000
_MAX_FIELD_BYTES = 65_536
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[0-9a-f]{32}")
_SIZE_MISMATCH = re.compile(r"size \d+ differs from C#'s \d+")
_AES_LENGTH = re.compile(r"AES (?:key|IV) of \d+ bytes")
_EXPECTED_SOURCE_REASONS = frozenset(
    {
        "no metadata blob",
        "metadata blob: not found",
        "no file size",
        "no segments",
        "negative volume size",
        "negative member offset",
        "negative member size",
        "negative offset",
        "negative length",
        "negative member range",
        "encrypted with no decoded size",
        "segment sizes unknown for a run that starts mid-file",
        "segments are not split evenly",
        "a part's data lies outside its segments",
        "RAR volume's first article is not its start",
        "RAR headers are encrypted",
        "member not found at the start of a RAR volume",
        "RAR member is compressed or encrypted",
        "an article belongs elsewhere in its file",
    }
)


class OmissionProofError(ValueError):
    """Static, credential-free refusal of incomplete or changed consent."""


def is_reviewable_reason(reason):
    return isinstance(reason, str) and (
        reason in _EXPECTED_SOURCE_REASONS
        or bool(_SIZE_MISMATCH.fullmatch(reason) or _AES_LENGTH.fullmatch(reason))
    )


def _canonical(value):
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError, RecursionError):
        raise OmissionProofError("The private omission proof is malformed.") from None
    if len(encoded) > MAX_OMISSION_PROOF_BYTES:
        raise OmissionProofError(
            "The complete private omission proof exceeds its storage bound."
        )
    return encoded


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _valid_census(census):
    return (
        type(census) is dict
        and set(census) == {"count", "sha256"}
        and type(census["count"]) is int
        and census["count"] >= 0
        and type(census["sha256"]) is str
        and _DIGEST.fullmatch(census["sha256"])
    )


def validate_omission_proof(proof):
    """Validate a complete private proof without normalizing or trusting samples."""
    if (
        type(proof) is not dict
        or set(proof)
        != {"policy", "source_media", "source_history", "omitted", "review_digest"}
        or proof["policy"] != PROOF_POLICY
        or not _valid_census(proof["source_media"])
        or not _valid_census(proof["source_history"])
        or type(proof["omitted"]) is not list
        or len(proof["omitted"]) > proof["source_media"]["count"]
        or type(proof["review_digest"]) is not str
        or not _DIGEST.fullmatch(proof["review_digest"])
    ):
        raise OmissionProofError(
            "The complete private omission proof is unavailable or malformed."
        )
    previous = None
    paths = set()
    for row in proof["omitted"]:
        if (
            type(row) is not dict
            or set(row) != {"id", "name", "path", "reason"}
            or any(
                type(row[field]) is not str
                for field in ("id", "name", "path", "reason")
            )
            or not _ID.fullmatch(row["id"])
            or (previous is not None and row["id"] <= previous)
            or not row["path"].startswith("/content/")
            or row["path"] in paths
            or any(
                len(row[field].encode("utf-8", "surrogatepass")) > _MAX_FIELD_BYTES
                for field in ("name", "path", "reason")
            )
            or any(ord(char) < 32 or ord(char) == 127 for char in row["path"])
            or not is_reviewable_reason(row["reason"])
        ):
            raise OmissionProofError(
                "The complete private omission records are malformed or unreviewable."
            )
        previous = row["id"]
        paths.add(row["path"])
    _canonical(proof)
    body = {key: value for key, value in proof.items() if key != "review_digest"}
    if not hmac.compare_digest(proof["review_digest"], _digest(body)):
        raise OmissionProofError("The private omission proof digest did not verify.")
    return proof


def make_omission_proof(source_media, source_history, omitted):
    """Bind full reconciled records to canonical source payload/history censuses."""
    try:
        rows = sorted((dict(row) for row in omitted), key=lambda row: row["id"])
        body = {
            "policy": PROOF_POLICY,
            "source_media": dict(source_media),
            "source_history": dict(source_history),
            "omitted": rows,
        }
    except (KeyError, TypeError, ValueError):
        raise OmissionProofError(
            "The private omission proof could not be constructed."
        ) from None
    proof = body | {"review_digest": _digest(body)}
    return validate_omission_proof(proof)


def verify_omission_subset(reviewed, actual):
    """Allow only the same losses or fewer losses for the same complete source."""
    validate_omission_proof(reviewed)
    validate_omission_proof(actual)
    if (
        reviewed["source_media"] != actual["source_media"]
        or reviewed["source_history"] != actual["source_history"]
    ):
        raise OmissionProofError(
            "The source media or history census changed after rehearsal; review a fresh rehearsal."
        )
    accepted = {row["id"]: row for row in reviewed["omitted"]}
    if any(accepted.get(row["id"]) != row for row in actual["omitted"]):
        raise OmissionProofError(
            "Cutover omitted an item or reason outside the exact reviewed omissions; review a fresh rehearsal."
        )
    return True
