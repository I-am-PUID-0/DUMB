"""Pinned native import diagnostics and identity reconciliation.

A native skip is an explained omission, never proof that an item can be repaired.
Only known deterministic source incompatibilities can be explicitly reviewed;
provider errors, unknown formats and unaccounted identities remain blockers.
"""

from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from urllib.parse import quote
import uuid

from utils.infinidysk_v2_omissions import (
    OmissionProofError,
    is_reviewable_reason as _reviewable,
    make_omission_proof,
)

MAX_REPORT_BYTES = 8_000_000
MAX_PUBLIC_SKIPPED = 200
MAX_PUBLIC_REASONS = 100
_SUMMARY = re.compile(
    r"Imported ([0-9]{1,20}) directories, ([0-9]{1,20}) files, ([0-9]{1,20}) NZBs, "
    r"([0-9]{1,20}) history rows, ([0-9]{1,20}) queued jobs"
)
_FETCHED = re.compile(r"  fetched ([0-9]{1,20}) articles to learn segment sizes")
_SKIPPED = re.compile(r"Files not carried over: ([1-9][0-9]{0,19})")
_GROUP = re.compile(r"  ([1-9][0-9]{0,19}): (.+)")


class ImportReportError(ValueError):
    """A report or inventory failed, with bounded operator-facing diagnostics."""

    def __init__(self, message, import_report=None):
        super().__init__(message)
        self.import_report = import_report or {
            "status": "blocked",
            "imported_files": None,
            "skipped_files": None,
            "review_required": False,
            "review_digest": None,
            "reasons": [],
            "skipped": [],
            "blocking_reasons": [message],
        }


def _safe_text(value):
    value = re.sub(
        r"(?i)(?:https?|postgres(?:ql)?|nntps?)://[^\s<>\"']+",
        "[URL omitted]",
        value,
    )
    value = re.sub(
        r"(?i)\b(password|token|secret|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        value,
    )
    return value[:1600]


def _public_report(parsed):
    reasons = parsed["reasons"]
    skipped = parsed["skipped"]
    return {
        "status": "accepted" if not skipped else "review_required",
        "imported_files": parsed["files"],
        "skipped_files": len(skipped),
        "review_required": bool(skipped),
        "review_digest": None,
        "reasons": [
            {"reason": _safe_text(reason), "count": count}
            for reason, count in reasons[:MAX_PUBLIC_REASONS]
        ],
        "reason_count": len(reasons),
        "reasons_truncated": len(reasons) > MAX_PUBLIC_REASONS,
        "skipped": [
            {"path": _safe_text(path), "reason": _safe_text(reason)}
            for path, reason in skipped[:MAX_PUBLIC_SKIPPED]
        ],
        "skipped_details_count": len(skipped),
        "skipped_details_truncated": len(skipped) > MAX_PUBLIC_SKIPPED,
        "blocking_reasons": [],
    }


def _import_tail(lines):
    summaries = [index for index, line in enumerate(lines) if _SUMMARY.fullmatch(line)]
    if len(summaries) != 1:
        raise ImportReportError(
            "The native import report summary is missing or ambiguous."
        )
    start = summaries[0]
    if not start:
        return lines
    # upgrade::describe() prepends Survey + Settings to Imported::Display.
    # Validate that pinned framing rather than accepting arbitrary leading text.
    prefix = lines[:start]
    patterns = (
        r"C# schema [^:]+: supported",
        r"Items: [0-9]+ directories; files: [0-9]+ NZB, [0-9]+ multipart, [0-9]+ RAR \(legacy\)",
        r"  decoded [0-9]+, failed [0-9]+, metadata blob missing [0-9]+, NZB blob missing [0-9]+",
        r"Import must:",
        r"  derive segment sizes for [0-9]+ segment runs that never recorded them",
        r"  resolve trailing RAR volumes of [0-9]+ files",
        r"  check [0-9]+ files whose ranges disagree with their size",
        r"Carried over:",
        r"  [0-9]+ encrypted files, decrypted on read",
        r"  [0-9]+ segment runs with unvalidated sizes",
        r"  [0-9]+ files with segments missing or corrupt everywhere",
        r"  [0-9]+ segment runs with fallback message-ids",
        r"  [0-9]+ files with PAR2 proofs",
        r"  [0-9]+ STRM outputs",
        r"History: [0-9]+ completed, [0-9]+ failed; queue: [0-9]+",
    )
    if len(prefix) < len(patterns) or any(
        not re.fullmatch(pattern, line) for pattern, line in zip(patterns, prefix)
    ):
        raise ImportReportError("The native survey preamble is unrecognized.")
    index = len(patterns)
    if index < len(prefix) and prefix[index].startswith("Failures ("):
        failure = re.fullmatch(
            r"Failures \(first ([0-9]{1,20}) of [0-9]{1,20}\):", prefix[index]
        )
        if not failure:
            raise ImportReportError(
                "The native survey failure framing is unrecognized."
            )
        count = int(failure[1])
        index += 1
        if count > len(prefix) - index or any(
            not re.fullmatch(r"  [^:]+: .+", line)
            for line in prefix[index : index + count]
        ):
            raise ImportReportError("The native survey failures are incomplete.")
        index += count
    settings = (
        "Settings:",
        "  admin login: ",
        "  SAB API key ",
        "  categories: ",
        "  mount dir: ",
        "  public URL: ",
        "  single sign-on: ",
        "  import strategy ",
        "  WebDAV user ",
        "  imports at once: ",
        "  health checks ",
        "  download limit: ",
    )
    if len(prefix) - index < len(settings) or any(
        line != expected if expected == "Settings:" else not line.startswith(expected)
        for expected, line in zip(settings, prefix[index:])
    ):
        raise ImportReportError("The native settings preamble is unrecognized.")
    index += len(settings)
    if any(
        not line.startswith(("  provider ", "  Arr ", "Note: "))
        for line in prefix[index:]
    ):
        raise ImportReportError(
            "The native report contains an unknown preamble section."
        )
    return lines[start:]


def parse_import_report(report):
    """Parse the verified beta.6 display contract without guessing unknown text."""
    if not isinstance(report, str) or len(report.encode()) > MAX_REPORT_BYTES:
        raise ImportReportError("The native import report is missing or too large.")
    lines = _import_tail(report.splitlines())
    if not lines or not (summary := _SUMMARY.fullmatch(lines[0])):
        raise ImportReportError("The native import report format is unrecognized.")
    values = [int(value) for value in summary.groups()]
    parsed = dict(zip(("directories", "files", "nzbs", "history", "queued"), values))
    parsed.update(fetched=0, skipped=[], reasons=[])
    index = 1
    # Historical DUMB executor fixtures used the summary alone. A report with
    # omissions must include the real native fetched line and complete groups.
    if index < len(lines) and (fetched := _FETCHED.fullmatch(lines[index])):
        parsed["fetched"] = int(fetched[1])
        index += 1
    elif index < len(lines):
        raise ImportReportError(
            "The native import report has an unknown article summary."
        )
    if index == len(lines):
        return parsed
    match = _SKIPPED.fullmatch(lines[index])
    if not match:
        raise ImportReportError("The native import report has an unknown section.")
    total = int(match[1])
    index += 1
    reasons = set()
    paths = set()
    while index < len(lines):
        group = _GROUP.fullmatch(lines[index])
        if not group:
            raise ImportReportError(
                "The native skipped-item reason section is incomplete."
            )
        count, reason = int(group[1]), group[2]
        if reason in reasons or any(ord(character) < 32 for character in reason):
            raise ImportReportError("The native skipped-item reasons are ambiguous.")
        reasons.add(reason)
        parsed["reasons"].append((reason, count))
        index += 1
        for _ in range(count):
            if index >= len(lines) or not lines[index].startswith("    /content/"):
                raise ImportReportError("The native skipped-item paths are incomplete.")
            path = lines[index][4:]
            if path in paths or any(ord(character) < 32 for character in path):
                raise ImportReportError("The native skipped-item paths are ambiguous.")
            paths.add(path)
            parsed["skipped"].append((path, reason))
            index += 1
    if len(parsed["skipped"]) != total:
        raise ImportReportError(
            "The native skipped-item total does not match its details."
        )
    return parsed


def _uuid(value):
    try:
        return (
            uuid.UUID(bytes=bytes(value)).hex
            if isinstance(value, (bytes, bytearray))
            else uuid.UUID(str(value)).hex
        )
    except (ValueError, AttributeError, TypeError) as error:
        raise ImportReportError(
            "A source or target media/history identity is unreadable."
        ) from error


def _block(public, message):
    public["status"] = "blocked"
    public["review_required"] = False
    public["review_digest"] = None
    public["blocking_reasons"].append(message)
    raise ImportReportError(message, public)


# These are native importer inputs, excluding changing health scheduling.
_MEDIA_FIELDS = (
    "Id",
    "ParentId",
    "Name",
    "Path",
    "Type",
    "SubType",
    "FileSize",
    "FileBlobId",
    "NzbBlobId",
    "CreatedAt",
    "ReleaseDate",
    "GeneratedStrmPath",
    "GeneratedSymlinkPath",
    "ArrDownloadId",
)
_HISTORY_FIELDS = (
    "Id",
    "DownloadStatus",
    "CreatedAt",
    "FileName",
    "JobName",
    "Category",
    "TotalSegmentBytes",
    "DownloadTimeSeconds",
    "FailMessage",
    "DownloadDirId",
    "NzbBlobId",
)


def _source_records(connection, table, fields):
    present = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    columns = [name for name in fields if name in present]
    required = {"Id", "Name", "Path", "Type"} if table == "DavItems" else {"Id"}
    if not required.issubset(columns):
        raise ImportReportError(
            "The source media/history census lacks required columns."
        )
    # All identifiers come from the fixed two callers above.
    projection = ",".join(f'"{name}"' for name in columns)
    rows = connection.execute(f'SELECT {projection} FROM "{table}"').fetchall()
    return columns, rows


def _blob_census(blob_root, value, cache):
    if value is None:
        return None
    try:
        identifier = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return {"state": "invalid_identity"}
    key = identifier.hex
    if key in cache:
        return cache[key]
    descriptors = []
    try:
        # Mirror native open_blob: the blobs root can be an explicit link;
        # descendants and the regular leaf must never follow links or block on FIFO.
        descriptor = os.open(blob_root, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(descriptor)
        for part in (key[:2], key[2:4]):
            descriptor = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            descriptors.append(descriptor)
        descriptor = os.open(
            str(identifier),
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=descriptor,
        )
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ImportReportError("A source payload blob is not a regular file.")
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        named = os.stat(str(identifier), dir_fd=descriptors[-2], follow_symlinks=False)
        stamp = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if (
            size != before.st_size
            or stamp(before) != stamp(after)
            or stamp(before) != stamp(named)
        ):
            raise ImportReportError(
                "A source payload blob changed during its complete census."
            )
        result = {"state": "present", "bytes": size, "sha256": digest.hexdigest()}
    except FileNotFoundError:
        result = {"state": "missing"}
    except OSError:
        raise ImportReportError(
            "A source payload blob could not be inspected without following links."
        ) from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    cache[key] = result
    return result


def _census(blob_root, columns, rows, cache, *, count):
    digest = hashlib.sha256()
    digest.update(json.dumps(columns, separators=(",", ":")).encode() + b"\n")
    entries = []
    for row in rows:
        identifier = _uuid(row[0])
        values = []
        for field, value in zip(columns, row):
            if field == "Id":
                value = identifier
            elif isinstance(value, bytes):
                value = {"bytes": value.hex()}
            values.append(value)
            if field in {"FileBlobId", "NzbBlobId"}:
                values.append(_blob_census(blob_root, value, cache))
        entries.append((identifier, values))
    for _identifier, values in sorted(entries, key=lambda row: row[0]):
        digest.update(
            json.dumps(
                values,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode()
            + b"\n"
        )
    return {"count": count, "sha256": digest.hexdigest()}


def verify_import_inventory(source, destination, report, *, source_blob_root=None):
    """Reconcile each imported identity and each exact, classified omission.

    Reviewable omissions return review_required; the caller must bind explicit
    user consent to review_digest before cutover. This function grants no consent.
    """
    parsed = parse_import_report(report)
    public = _public_report(parsed)
    try:
        with closing(
            sqlite3.connect(f"file:{quote(str(Path(source)))}?mode=ro", uri=True)
        ) as old:
            media_columns, media_rows = _source_records(old, "DavItems", _MEDIA_FIELDS)
            files = old.execute(
                "SELECT Id, Name, Path FROM DavItems "
                "WHERE Type=2 AND Path LIKE '/content/%'"
            ).fetchall()
            history_columns, history_rows_source = _source_records(
                old, "HistoryItems", _HISTORY_FIELDS
            )
            history = [(row[0],) for row in history_rows_source]
            history_summary = {
                "source_rows": len(history),
                "completed": 0,
                "failed": 0,
                "unsupported_status": 0,
                "invalid_identity": 0,
                "imported_this_attempt": parsed["history"],
            }
            if history and "DownloadStatus" not in history_columns:
                _block(
                    public,
                    "Source history eligibility cannot be verified: DownloadStatus is missing.",
                )
            status_index = history_columns.index("DownloadStatus") if history else None
            for row in history_rows_source:
                status = row[status_index]
                if type(status) is not int or status not in {1, 2}:
                    history_summary["unsupported_status"] += 1
                else:
                    history_summary["completed" if status == 1 else "failed"] += 1
                try:
                    identifier = int(_uuid(row[0]), 16)
                    if identifier <= 4 and identifier != 2:
                        history_summary["invalid_identity"] += 1
                except ImportReportError:
                    history_summary["invalid_identity"] += 1
            public["history"] = history_summary
            if (
                history_summary["unsupported_status"]
                or history_summary["invalid_identity"]
            ):
                _block(
                    public,
                    "Source history contains "
                    f"{history_summary['unsupported_status']} unsupported status row(s) and "
                    f"{history_summary['invalid_identity']} invalid/reserved identity row(s). "
                    "The pinned native importer cannot preserve those rows; review the source history before migrating.",
                )
            blob_root = Path(source).parent / "blobs"
            if source_blob_root is not None:
                blob_root = Path(source_blob_root)
                if not blob_root.is_absolute() or ".." in blob_root.parts:
                    _block(
                        public,
                        "The explicit source blob census requires its proven absolute staged directory.",
                    )
                try:
                    if any(
                        path.is_symlink() for path in (blob_root, *blob_root.parents)
                    ):
                        _block(
                            public,
                            "The explicit source blob census requires a direct, scope-verified physical directory.",
                        )
                    try:
                        blob_stat = blob_root.lstat()
                    except FileNotFoundError:
                        blob_stat = None
                    if blob_stat is not None and not stat.S_ISDIR(blob_stat.st_mode):
                        _block(
                            public,
                            "The explicit source blob census directory is unavailable.",
                        )
                    if blob_stat is None:
                        for columns, records in (
                            (media_columns, media_rows),
                            (history_columns, history_rows_source),
                        ):
                            for field in ("FileBlobId", "NzbBlobId"):
                                if field not in columns:
                                    continue
                                index = columns.index(field)
                                for record in records:
                                    if record[index] is None:
                                        continue
                                    try:
                                        uuid.UUID(str(record[index]))
                                    except (ValueError, AttributeError, TypeError):
                                        continue
                                    _block(
                                        public,
                                        "The explicit source blob census directory is missing despite referenced payload blobs.",
                                    )
                except OSError:
                    _block(
                        public,
                        "The explicit source blob census directory could not be inspected safely.",
                    )
            blob_cache = {}
            media_census = _census(
                blob_root, media_columns, media_rows, blob_cache, count=len(files)
            )
            history_census = _census(
                blob_root,
                history_columns,
                history_rows_source,
                blob_cache,
                count=len(history),
            )
        with closing(
            sqlite3.connect(f"file:{quote(str(Path(destination)))}?mode=ro", uri=True)
        ) as new:
            if new.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                _block(public, "Native database integrity did not validate.")
            item_rows = new.execute("SELECT id,name,kind FROM items").fetchall()
            history_rows = new.execute("SELECT id FROM history").fetchall()
        items = {
            _uuid(identifier): (name, kind) for identifier, name, kind in item_rows
        }
        actual_history = {_uuid(row[0]) for row in history_rows}
        source_ids = [_uuid(row[0]) for row in files]
        old_history = [_uuid(row[0]) for row in history]
    except ImportReportError as error:
        if error.import_report.get("imported_files") is None:
            _block(public, str(error))
        raise
    except sqlite3.Error as error:
        _block(
            public,
            f"The native/source inventory could not be read: {type(error).__name__}.",
        )
    except (TypeError, ValueError):
        _block(
            public, "The source media/history census could not be represented safely."
        )

    if any(kind not in {1, 2, 3} for _, _, kind in item_rows):
        _block(public, "The native target contains an unrecognized media kind.")
    if len(set(source_ids)) != len(source_ids) or len(items) != len(item_rows):
        _block(public, "Duplicate native/source media identities are ambiguous.")
    if (
        len(set(old_history)) != len(old_history)
        or len(actual_history) != len(history_rows)
        or parsed["history"] > len(history)
        or not set(old_history).issubset(actual_history)
    ):
        missing_history = set(old_history) - actual_history
        statuses = {_uuid(row[0]): row[status_index] for row in history_rows_source}
        missing_completed = sum(statuses[item] == 1 for item in missing_history)
        missing_failed = sum(statuses[item] == 2 for item in missing_history)
        public["history"].update(
            missing_completed=missing_completed, missing_failed=missing_failed
        )
        _block(
            public,
            "Existing history identities or totals were not preserved: "
            f"{missing_completed} completed and {missing_failed} failed required history identities missing; "
            f"source {len(history)}, target {len(history_rows)}, "
            f"native attempt {parsed['history']}. Completed and failed history must both be retained.",
        )
    public["history"]["verified_present"] = len(old_history)
    public["history"]["attempt_count_differs"] = parsed["history"] != len(history)
    if parsed["queued"] != 0:
        _block(
            public, "The native import contains queued jobs; empty the source queue."
        )

    skips = dict(parsed["skipped"])
    missing = []
    carried = 0
    source_paths = Counter(row[2] for row in files)
    for identifier, (_, name, path) in zip(source_ids, files):
        if identifier in items:
            if items[identifier][0] != name:
                _block(public, "Existing media names were not preserved.")
            if items[identifier][1] not in {2, 3}:
                _block(public, "An existing media file became a non-file native item.")
            if path in skips:
                _block(
                    public, "A native skipped path also exists in the imported media."
                )
            carried += 1
        else:
            if path is None or source_paths[path] != 1 or path not in skips:
                _block(
                    public,
                    "Missing media identities are not explained by unique native skipped paths.",
                )
            missing.append(
                {"id": identifier, "name": name, "path": path, "reason": skips[path]}
            )
    if len(missing) != len(skips) or parsed["files"] != carried:
        _block(
            public,
            "Native import totals or skipped directory/file identities do not reconcile.",
        )
    blocked = sorted(
        {item["reason"] for item in missing if not _reviewable(item["reason"])}
    )
    if blocked:
        public["blocking_reasons"] = [
            "Provider access, unsupported metadata recovery or an unknown skip reason requires attention: "
            + _safe_text(reason)
            for reason in blocked[:MAX_PUBLIC_REASONS]
        ]
        public["status"] = "blocked"
        public["review_required"] = False
        raise ImportReportError(
            f"The native import omitted {len(missing)} items; provider or unknown failures block cutover.",
            public,
        )
    try:
        proof = make_omission_proof(media_census, history_census, missing)
    except OmissionProofError as error:
        _block(public, str(error))
    if missing:
        public["review_digest"] = proof["review_digest"]
    return {
        "files": carried,
        "history": len(history),
        "skipped_files": len(missing),
        "failed_files": 0,
        "native_report_sha256": hashlib.sha256(report.encode()).hexdigest(),
        "omission_proof": proof,
        "import_report": public,
    }
