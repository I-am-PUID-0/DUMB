"""Pinned native import diagnostics and identity reconciliation.

A native skip is an explained omission, never proof that an item can be repaired.
Only known deterministic source incompatibilities can be explicitly reviewed;
provider errors, unknown formats and unaccounted identities remain blockers.
"""

from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote
import uuid

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


def _reviewable(reason):
    return reason in _EXPECTED_SOURCE_REASONS or bool(
        _SIZE_MISMATCH.fullmatch(reason) or _AES_LENGTH.fullmatch(reason)
    )


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


def verify_import_inventory(source, destination, report):
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
            files = old.execute(
                "SELECT Id, Name, Path FROM DavItems "
                "WHERE Type=2 AND Path LIKE '/content/%'"
            ).fetchall()
            history = old.execute("SELECT Id FROM HistoryItems").fetchall()
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

    if any(kind not in {1, 2, 3} for _, _, kind in item_rows):
        _block(public, "The native target contains an unrecognized media kind.")
    if len(set(source_ids)) != len(source_ids) or len(items) != len(item_rows):
        _block(public, "Duplicate native/source media identities are ambiguous.")
    if (
        len(set(old_history)) != len(old_history)
        or len(actual_history) != len(history_rows)
        or parsed["history"] != len(history)
        or not set(old_history).issubset(actual_history)
    ):
        _block(public, "Existing history identities or totals were not preserved.")
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
    if missing:
        binding = {
            "policy": "infinidysk-beta6-reviewed-omissions-v1",
            "source_files": len(files),
            "history": len(history),
            "omitted": sorted(missing, key=lambda item: item["id"]),
        }
        public["review_digest"] = hashlib.sha256(
            json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    return {
        "files": carried,
        "history": len(history),
        "skipped_files": len(missing),
        "failed_files": 0,
        "native_report_sha256": hashlib.sha256(report.encode()).hexdigest(),
        "import_report": public,
    }
