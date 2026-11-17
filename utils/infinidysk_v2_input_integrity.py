"""Authored migration-input diagnostics without paths or database values."""

import hashlib
import os
import re
import stat

FIELDS = ("dev", "inode", "mode", "size", "mtime_ns", "ctime_ns", "nlink")
CODES = {"recorded_entry_changed", "input_content_mismatch"}


def public_input_integrity(value):
    if not isinstance(value, dict):
        return None
    if (
        not isinstance(value.get("code"), str)
        or value["code"] not in CODES
        or value.get("side") not in ("source", "staged")
        or value.get("entry_type") not in ("file", "directory", "other")
        or not isinstance(value.get("path_digest"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["path_digest"])
        or not isinstance(value.get("changed_fields"), list)
    ):
        return None
    fields = value["changed_fields"]
    if not fields or any(
        not isinstance(item, str) or item not in (*FIELDS, "sha256") for item in fields
    ):
        return None
    return {
        key: value[key] for key in ("code", "side", "entry_type", "path_digest")
    } | {"changed_fields": list(dict.fromkeys(fields))}


def input_change(relative, mode, *, staged, changed_fields, content=False):
    return {
        "code": "input_content_mismatch" if content else "recorded_entry_changed",
        "side": "staged" if staged else "source",
        "entry_type": (
            "file"
            if stat.S_ISREG(mode)
            else "directory" if stat.S_ISDIR(mode) else "other"
        ),
        "path_digest": hashlib.sha256(os.fsencode(relative)).hexdigest(),
        "changed_fields": list(changed_fields),
    }


def integrity_message(evidence):
    side = "External staged" if evidence["side"] == "staged" else "Local source"
    fields = ", ".join(evidence["changed_fields"])
    return f"{side} migration input {evidence['entry_type']} did not verify (changed fields: {fields}). Altered input was not accepted."
