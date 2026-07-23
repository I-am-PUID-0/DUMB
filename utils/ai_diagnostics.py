from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import json
import math
import os
import re
import sqlite3
import statistics
import time

from utils.logger import redact_sensitive_log_data

_SECRET_HINTS = (
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "authorization",
    "client_secret",
    "plex_token",
    "github_token",
    "tunnel_token",
)
_LOG_TIMESTAMP = re.compile(
    r"^(?P<stamp>[A-Z][a-z]{2} \d{1,2}, \d{4} \d{2}:\d{2}:\d{2})"
)
_ISO_TIMESTAMP = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+(?:Z)?)")
_OUTER_LEVEL = re.compile(r"\s-\s(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s-\s")
_INNER_LEVEL = re.compile(r"\[[^\]]*\s(?P<level>DBG|INF|WRN|ERR|FTL)\]")
_GUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.I,
)
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_SPACE = re.compile(r"\s+")
_RESTART_MARKERS = (
    "application started",
    "now listening on",
    "started process",
    "service started",
)
_STOP_MARKERS = (
    "application is shutting down",
    "stopped process",
    "service stopped",
)


def _utc_iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _safe_value(path: str, value: Any) -> Any:
    lowered = path.lower()
    if any(hint in lowered for hint in _SECRET_HINTS):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_sensitive_log_data(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_sensitive_log_data(json.dumps(value, sort_keys=True))[:500]


def _redact_structure(value: Any, path: str = "") -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_structure(
                child,
                f"{path}.{key}" if path else str(key),
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_structure(child, path) for child in value]
    return _safe_value(path, value)


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(_flatten(child, path))
        return flattened
    if isinstance(value, list):
        flattened[prefix] = value
        return flattened
    flattened[prefix] = value
    return flattened


class DiagnosticEventStore:
    """Small redacted event ledger used for before/after diagnostic boundaries."""

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get(
            "DUMB_AI_DIAGNOSTICS_DB",
            "/config/ai-diagnostics/events.sqlite",
        )

    def _connect(self) -> sqlite3.Connection:
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=2)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=2000")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS diagnostic_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at REAL NOT NULL,
                process_name TEXT,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                actor TEXT,
                summary TEXT NOT NULL,
                details_json TEXT NOT NULL
            )
            """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS ix_diagnostic_events_process_at
            ON diagnostic_events(process_name, at)
            """)
        return connection

    def record(
        self,
        event_type: str,
        source: str,
        summary: str,
        *,
        process_name: str | None = None,
        actor: str | None = None,
        details: dict | None = None,
        at: float | None = None,
    ) -> None:
        safe_details = _redact_structure(details or {})
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO diagnostic_events
                    (at, process_name, event_type, source, actor, summary, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(at or time.time()),
                    process_name,
                    event_type,
                    source,
                    "authenticated" if actor else None,
                    redact_sensitive_log_data(summary)[:1000],
                    json.dumps(safe_details, separators=(",", ":"), sort_keys=True),
                ),
            )

    def record_config_change(
        self,
        before: Any,
        after: Any,
        *,
        process_name: str | None,
        actor: str | None,
        source: str,
    ) -> int:
        old = _flatten(before)
        new = _flatten(after)
        changed = []
        for path in sorted(set(old) | set(new)):
            if old.get(path) == new.get(path):
                continue
            changed.append(
                {
                    "path": path,
                    "before": _safe_value(path, old.get(path)),
                    "after": _safe_value(path, new.get(path)),
                }
            )
        if not changed:
            return 0
        self.record(
            "config_change",
            source,
            f"{len(changed)} configuration value(s) changed",
            process_name=process_name,
            actor=actor,
            details={
                "changes": changed[:100],
                "truncated": len(changed) > 100,
                "total_changes": len(changed),
            },
        )
        return len(changed)

    def list(
        self,
        *,
        process_name: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 200,
    ) -> list[dict]:
        clauses = []
        params: list[Any] = []
        if process_name:
            clauses.append("(process_name = ? OR process_name IS NULL)")
            params.append(process_name)
        if since is not None:
            clauses.append("at >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("at < ?")
            params.append(float(until))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT id, at, process_name, event_type, source, actor,
                           summary, details_json
                    FROM diagnostic_events
                    {where}
                    ORDER BY at DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
        except (OSError, sqlite3.Error):
            return []
        events = []
        for row in rows:
            try:
                details = json.loads(row[7] or "{}")
            except json.JSONDecodeError:
                details = {}
            events.append(
                {
                    "id": row[0],
                    "at": _utc_iso(row[1]),
                    "timestamp": row[1],
                    "process_name": row[2],
                    "event_type": row[3],
                    "source": row[4],
                    "actor": row[5],
                    "summary": row[6],
                    "details": details,
                }
            )
        return events


def record_config_change(
    before: Any,
    after: Any,
    *,
    process_name: str | None,
    actor: str | None,
    source: str,
    logger=None,
) -> None:
    try:
        DiagnosticEventStore().record_config_change(
            before,
            after,
            process_name=process_name,
            actor=actor,
            source=source,
        )
    except Exception as exc:
        debug = getattr(logger, "debug", None)
        if callable(debug):
            debug("AI diagnostic config event could not be recorded: %s", exc)


def record_diagnostic_event(
    event_type: str,
    summary: str,
    *,
    process_name: str | None,
    actor: str | None,
    source: str = "operator_action",
    details: dict | None = None,
    logger=None,
) -> None:
    try:
        DiagnosticEventStore().record(
            event_type,
            source,
            summary,
            process_name=process_name,
            actor=actor,
            details=details,
        )
    except Exception as exc:
        debug = getattr(logger, "debug", None)
        if callable(debug):
            debug("AI diagnostic event could not be recorded: %s", exc)


def _parse_timestamp(line: str) -> float | None:
    match = _LOG_TIMESTAMP.match(line)
    if match:
        try:
            parsed = datetime.strptime(
                match.group("stamp"), "%b %d, %Y %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return None
    match = _ISO_TIMESTAMP.match(line)
    if not match:
        return None
    stamp = match.group("stamp").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _line_level(line: str) -> str:
    inner = _INNER_LEVEL.search(line)
    if inner:
        return {
            "DBG": "debug",
            "INF": "info",
            "WRN": "warning",
            "ERR": "error",
            "FTL": "critical",
        }.get(inner.group("level"), "info")
    outer = _OUTER_LEVEL.search(line)
    if outer:
        return outer.group("level").lower()
    lowered = line.lower()
    if "traceback" in lowered or "exception" in lowered or " failed" in lowered:
        return "error"
    return "info"


def _signature(line: str) -> str:
    text = redact_sensitive_log_data(line)
    text = _GUID.sub("<id>", text)
    text = _NUMBER.sub("<n>", text)
    text = _SPACE.sub(" ", text).strip()
    if " - " in text:
        text = text.split(" - ", 3)[-1]
    return text[:240]


def discover_log_files(path: Path, max_files: int = 30) -> list[Path]:
    parent = path.parent
    name = path.name
    stem = name[:-4] if name.endswith(".log") else path.stem
    if re.search(r"-\d{4}-\d{2}-\d{2}(?:_\d+)?$", stem):
        stem = re.sub(r"-\d{4}-\d{2}-\d{2}(?:_\d+)?$", "", stem)
    candidates = []
    for candidate in parent.glob(f"{stem}*.log"):
        if candidate.is_file():
            candidates.append(candidate)
    if path.is_file() and path not in candidates:
        candidates.append(path)
    return sorted(
        candidates,
        key=lambda candidate: candidate.stat().st_mtime,
        reverse=True,
    )[: max(1, max_files)]


def scan_retained_logs(
    path: Path,
    *,
    since: float,
    until: float,
    question: str = "",
    max_scan_mb: int = 128,
    max_excerpts: int = 40,
) -> dict:
    files = discover_log_files(path)
    byte_limit = max(1, min(int(max_scan_mb), 1024)) * 1024 * 1024
    selected = []
    offsets: dict[Path, int] = {}
    selected_bytes = 0
    partial_file = False
    for candidate in files:
        size = candidate.stat().st_size
        remaining = byte_limit - selected_bytes
        if remaining <= 0:
            break
        if size > remaining:
            if selected:
                break
            offsets[candidate] = size - remaining
            partial_file = True
            selected.append(candidate)
            selected_bytes += remaining
            break
        offsets[candidate] = 0
        selected.append(candidate)
        selected_bytes += size
        if selected_bytes >= byte_limit:
            break

    levels = Counter()
    signatures = Counter()
    first_half_signatures = Counter()
    second_half_signatures = Counter()
    restart_count = 0
    stop_count = 0
    lines_scanned = 0
    lines_in_window = 0
    timestamps = []
    excerpts = []
    midpoint = since + ((until - since) / 2)
    keywords = {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", question or "")
        if token.lower()
        not in {"what", "this", "that", "with", "from", "service", "running"}
    }

    for candidate in reversed(selected):
        try:
            with candidate.open("rb") as handle:
                offset = offsets.get(candidate, 0)
                if offset:
                    handle.seek(offset)
                    handle.readline()
                for line_number, raw_line in enumerate(handle, start=1):
                    line = raw_line.decode("utf-8", errors="replace")
                    lines_scanned += 1
                    timestamp = _parse_timestamp(line)
                    if timestamp is None or timestamp < since or timestamp >= until:
                        continue
                    lines_in_window += 1
                    timestamps.append(timestamp)
                    level = _line_level(line)
                    levels[level] += 1
                    lowered = line.lower()
                    if any(marker in lowered for marker in _RESTART_MARKERS):
                        restart_count += 1
                    if any(marker in lowered for marker in _STOP_MARKERS):
                        stop_count += 1
                    if level in {"warning", "error", "critical"}:
                        signature = _signature(line)
                        signatures[signature] += 1
                        target = (
                            first_half_signatures
                            if timestamp < midpoint
                            else second_half_signatures
                        )
                        target[signature] += 1
                    keyword_match = bool(
                        keywords and any(k in lowered for k in keywords)
                    )
                    if (
                        level in {"error", "critical"}
                        or keyword_match
                        or (level == "warning" and len(excerpts) < max_excerpts // 2)
                    ):
                        excerpts.append(
                            {
                                "at": _utc_iso(timestamp),
                                "level": level,
                                "file": candidate.name,
                                "line": line_number,
                                "content": redact_sensitive_log_data(line.strip())[
                                    :1000
                                ],
                            }
                        )
        except OSError:
            continue

    excerpts = excerpts[-max(1, min(int(max_excerpts), 200)) :]
    gaps = []
    ordered = sorted(set(timestamps))
    for left, right in zip(ordered, ordered[1:]):
        gap = right - left
        if gap >= 300:
            gaps.append(gap)
    new_signatures = [
        {"signature": signature, "count": count}
        for signature, count in second_half_signatures.most_common()
        if signature not in first_half_signatures
    ][:10]
    top_signatures = [
        {"signature": signature, "count": count}
        for signature, count in signatures.most_common(15)
    ]

    return {
        "coverage": {
            "available": True,
            "files_discovered": len(files),
            "files_scanned": len(selected),
            "bytes_scanned": selected_bytes,
            "lines_scanned": lines_scanned,
            "lines_in_window": lines_in_window,
            "all_retained_files_scanned": (
                len(selected) == len(files) and not partial_file
            ),
            "truncated": len(selected) != len(files) or partial_file,
            "partial_file_scanned": partial_file,
            "window_start": _utc_iso(since),
            "window_end": _utc_iso(until),
        },
        "levels": dict(levels),
        "restart_markers": restart_count,
        "stop_markers": stop_count,
        "top_error_signatures": top_signatures,
        "new_error_signatures": new_signatures,
        "longest_activity_gap_seconds": round(max(gaps), 1) if gaps else None,
        "excerpts": excerpts,
        "_files": [str(candidate) for candidate in selected],
        "_file_ranges": [
            {"path": str(candidate), "offset": offsets.get(candidate, 0)}
            for candidate in selected
        ],
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    return float(
        ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    )


def _summary(values: list[float]) -> dict:
    if not values:
        return {"samples": 0}
    return {
        "samples": len(values),
        "average": round(statistics.fmean(values), 3),
        "median": round(statistics.median(values), 3),
        "p95": round(_percentile(values, 0.95) or 0, 3),
        "maximum": round(max(values), 3),
        "minimum": round(min(values), 3),
    }


def _process_history_stats(
    items: list[dict], process_name: str, since: float, until: float
) -> dict:
    cpu = []
    rss = []
    disk_read = []
    disk_write = []
    timestamps = []
    pids = set()
    missing_samples = 0
    for item in items:
        timestamp = item.get("timestamp")
        if timestamp is None or timestamp < since or timestamp >= until:
            continue
        match = next(
            (
                process
                for process in (item.get("dumb_managed") or [])
                if str(process.get("name") or "").casefold() == process_name.casefold()
            ),
            None,
        )
        if not match:
            missing_samples += 1
            continue
        timestamps.append(float(timestamp))
        if match.get("pid") is not None:
            pids.add(match.get("pid"))
        if match.get("cpu_percent") is not None:
            cpu.append(float(match["cpu_percent"]))
        if match.get("rss") is not None:
            rss.append(float(match["rss"]))
        disk = match.get("disk_io") or {}
        if disk.get("read_bytes") is not None:
            disk_read.append((float(timestamp), float(disk["read_bytes"])))
        if disk.get("write_bytes") is not None:
            disk_write.append((float(timestamp), float(disk["write_bytes"])))

    def _counter_rate(points: list[tuple[float, float]]) -> float | None:
        if len(points) < 2:
            return None
        points.sort()
        delta = points[-1][1] - points[0][1]
        elapsed = points[-1][0] - points[0][0]
        if elapsed <= 0 or delta < 0:
            return None
        return round(delta / elapsed, 3)

    return {
        "sample_count": len(timestamps),
        "missing_sample_count": missing_samples,
        "coverage_start": _utc_iso(min(timestamps)) if timestamps else None,
        "coverage_end": _utc_iso(max(timestamps)) if timestamps else None,
        "observed_pids": sorted(pids),
        "restart_indications": max(0, len(pids) - 1),
        "cpu_percent": _summary(cpu),
        "rss_bytes": _summary(rss),
        "disk_read_bytes_per_second": _counter_rate(disk_read),
        "disk_write_bytes_per_second": _counter_rate(disk_write),
    }


def build_runtime_comparison(
    history_manager,
    process_name: str,
    *,
    since: float,
    until: float,
    comparison_since: float | None,
    comparison_until: float | None,
) -> dict:
    read_since = comparison_since if comparison_since is not None else since
    try:
        items, truncated = history_manager.read(
            since=read_since,
            full=False,
            limit=50000,
            default_hours=max(1, int((until - read_since) / 3600)),
        )
    except Exception as exc:
        return {
            "available": False,
            "reason": f"Metrics history unavailable: {type(exc).__name__}",
        }
    current = _process_history_stats(items, process_name, since, until)
    result = {
        "available": current["sample_count"] > 0,
        "truncated": bool(truncated),
        "current": current,
    }
    if comparison_since is not None and comparison_until is not None:
        baseline = _process_history_stats(
            items, process_name, comparison_since, comparison_until
        )
        result["baseline"] = baseline
        result["changes"] = _metric_changes(current, baseline)
    return result


def build_stack_runtime_comparison(
    history_manager,
    process_names: list[str],
    *,
    since: float,
    until: float,
    comparison_since: float | None,
    comparison_until: float | None,
) -> dict:
    read_since = comparison_since if comparison_since is not None else since
    try:
        items, truncated = history_manager.read(
            since=read_since,
            full=False,
            limit=50000,
            default_hours=max(1, int((until - read_since) / 3600)),
        )
    except Exception as exc:
        return {
            "available": False,
            "reason": f"Metrics history unavailable: {type(exc).__name__}",
            "services": {},
        }
    services = {}
    for process_name in process_names:
        current = _process_history_stats(items, process_name, since, until)
        payload = {
            "available": current["sample_count"] > 0,
            "current": current,
        }
        if comparison_since is not None and comparison_until is not None:
            baseline = _process_history_stats(
                items,
                process_name,
                comparison_since,
                comparison_until,
            )
            payload["baseline"] = baseline
            payload["changes"] = _metric_changes(current, baseline)
        services[process_name] = payload
    return {
        "available": any(item["available"] for item in services.values()),
        "truncated": bool(truncated),
        "services": services,
    }


def _metric_changes(current: dict, baseline: dict) -> dict:
    changes = {}
    for key in ("cpu_percent", "rss_bytes"):
        current_value = (current.get(key) or {}).get("average")
        baseline_value = (baseline.get(key) or {}).get("average")
        if current_value is None or baseline_value in (None, 0):
            continue
        changes[f"{key}_average_percent"] = round(
            ((current_value - baseline_value) / baseline_value) * 100,
            2,
        )
    for key in ("disk_read_bytes_per_second", "disk_write_bytes_per_second"):
        current_value = current.get(key)
        baseline_value = baseline.get(key)
        if current_value is None or baseline_value in (None, 0):
            continue
        changes[f"{key}_percent"] = round(
            ((current_value - baseline_value) / baseline_value) * 100,
            2,
        )
    return changes


def resolve_windows(
    *,
    window_hours: float,
    comparison: str,
    events: list[dict],
    now: float | None = None,
) -> dict:
    until = float(now or time.time())
    hours = max(0.25, min(float(window_hours or 24), 24 * 30))
    duration = hours * 3600
    since = until - duration
    comparison_since = None
    comparison_until = None
    comparison_mode = str(comparison or "previous_period").lower()
    boundary_event = None
    if comparison_mode == "previous_period":
        comparison_until = since
        comparison_since = since - duration
    elif comparison_mode == "since_change":
        boundary_event = next(
            (
                event
                for event in events
                if event.get("event_type") == "config_change"
                and event.get("timestamp")
                and event["timestamp"] < until
            ),
            None,
        )
        if boundary_event:
            since = float(boundary_event["timestamp"])
            duration = max(900, until - since)
            comparison_until = since
            comparison_since = since - duration
        else:
            comparison_mode = "previous_period"
            comparison_until = since
            comparison_since = since - duration
    elif comparison_mode in {"none", "off"}:
        comparison_mode = "none"
    else:
        comparison_mode = "previous_period"
        comparison_until = since
        comparison_since = since - duration
    return {
        "current": {"since": since, "until": until},
        "baseline": (
            {"since": comparison_since, "until": comparison_until}
            if comparison_since is not None
            else None
        ),
        "comparison": comparison_mode,
        "boundary_event": boundary_event,
    }


def _sqlite_connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        timeout=2,
    )
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=1500")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _nzbdav_config(base: Path) -> dict:
    path = base / "db.sqlite"
    if not path.is_file():
        return {"available": False, "reason": "NzbDAV config database not found."}
    try:
        with _sqlite_connect_readonly(path) as connection:
            if not _table_exists(connection, "ConfigItems"):
                return {
                    "available": False,
                    "reason": "NzbDAV ConfigItems table is unavailable.",
                }
            rows = dict(connection.execute("""
                    SELECT ConfigName, ConfigValue
                    FROM ConfigItems
                    WHERE ConfigName IN (
                        'queue.worker-count',
                        'usenet.max-queue-connections',
                        'usenet.providers',
                        'usenet.streaming-priority',
                        'usenet.pipelining.enabled',
                        'usenet.pipelining.depth'
                    )
                    """).fetchall())
    except (OSError, sqlite3.Error) as exc:
        return {
            "available": False,
            "reason": f"NzbDAV config query failed: {type(exc).__name__}",
        }
    total_pool = None
    provider_count = None
    try:
        providers = json.loads(rows.get("usenet.providers") or "{}")
        total_pool = providers.get("TotalPooledConnections")
        provider_count = len(providers.get("Providers") or [])
    except (TypeError, ValueError):
        pass
    return {
        "available": True,
        "queue_worker_count": int(rows.get("queue.worker-count") or 1),
        "max_queue_connections": (
            int(rows["usenet.max-queue-connections"])
            if str(rows.get("usenet.max-queue-connections") or "").isdigit()
            else total_pool
        ),
        "total_pooled_connections": total_pool,
        "provider_count": provider_count,
        "streaming_priority": rows.get("usenet.streaming-priority"),
        "pipelining_enabled": rows.get("usenet.pipelining.enabled"),
        "pipelining_depth": rows.get("usenet.pipelining.depth"),
    }


def _nzbdav_metric_window(
    connection: sqlite3.Connection, since: float, until: float
) -> dict:
    start_ms = int(since * 1000)
    end_ms = int(until * 1000)
    result: dict[str, Any] = {}
    if _table_exists(connection, "SegmentFetches"):
        row = connection.execute(
            """
            SELECT
                COUNT(*),
                SUM(CASE WHEN Status = 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN Status = 1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN Status NOT IN (0, 1) THEN 1 ELSE 0 END),
                SUM(Retries),
                AVG(CASE WHEN Status = 0 THEN DurationMs END)
            FROM SegmentFetches
            WHERE At >= ? AND At < ?
            """,
            (start_ms, end_ms),
        ).fetchone()
        total = int(row[0] or 0)
        result["segment_fetches"] = {
            "count": total,
            "ok": int(row[1] or 0),
            "missing": int(row[2] or 0),
            "errors": int(row[3] or 0),
            "retries": int(row[4] or 0),
            "missing_percent": round((row[2] or 0) * 100 / total, 3) if total else None,
            "error_percent": round((row[3] or 0) * 100 / total, 3) if total else None,
            "retries_per_100": round((row[4] or 0) * 100 / total, 3) if total else None,
            "successful_average_ms": (
                round(float(row[5] or 0), 2) if row[5] is not None else None
            ),
        }
        read_row = connection.execute(
            """
            SELECT
                COUNT(*),
                SUM(CASE WHEN Status = 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN Status = 1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN Status NOT IN (0, 1) THEN 1 ELSE 0 END),
                SUM(Retries),
                AVG(CASE WHEN Status = 0 THEN DurationMs END)
            FROM SegmentFetches
            WHERE At >= ? AND At < ? AND ReadSessionId IS NOT NULL
            """,
            (start_ms, end_ms),
        ).fetchone()
        read_total = int(read_row[0] or 0)
        result["read_segment_fetches"] = {
            "count": read_total,
            "missing_percent": (
                round((read_row[2] or 0) * 100 / read_total, 3) if read_total else None
            ),
            "error_percent": (
                round((read_row[3] or 0) * 100 / read_total, 3) if read_total else None
            ),
            "retries_per_100": (
                round((read_row[4] or 0) * 100 / read_total, 3) if read_total else None
            ),
            "successful_average_ms": (
                round(float(read_row[5] or 0), 2) if read_row[5] is not None else None
            ),
        }
    if _table_exists(connection, "ReadSessions"):
        row = connection.execute(
            """
            SELECT
                COUNT(*),
                SUM(BytesServed),
                SUM(BytesFetched),
                AVG(DurationMs),
                SUM(CASE WHEN EndReason = 3 THEN 1 ELSE 0 END)
            FROM ReadSessions
            WHERE StartedAt >= ? AND StartedAt < ?
            """,
            (start_ms, end_ms),
        ).fetchone()
        total = int(row[0] or 0)
        result["read_sessions"] = {
            "count": total,
            "bytes_served": int(row[1] or 0),
            "bytes_fetched": int(row[2] or 0),
            "average_duration_ms": (
                round(float(row[3] or 0), 2) if row[3] is not None else None
            ),
            "errors": int(row[4] or 0),
            "error_percent": round((row[4] or 0) * 100 / total, 3) if total else None,
        }
    return result


def _metric_window_changes(current: dict, baseline: dict) -> dict:
    changes = {}
    for section in ("segment_fetches", "read_segment_fetches", "read_sessions"):
        left = current.get(section) or {}
        right = baseline.get(section) or {}
        section_changes = {}
        for metric in (
            "successful_average_ms",
            "missing_percent",
            "error_percent",
            "retries_per_100",
            "average_duration_ms",
        ):
            current_value = left.get(metric)
            baseline_value = right.get(metric)
            if current_value is None or baseline_value in (None, 0):
                continue
            section_changes[f"{metric}_percent"] = round(
                ((current_value - baseline_value) / baseline_value) * 100,
                2,
            )
        if section_changes:
            changes[section] = section_changes
    return changes


def _nzbdav_log_window(files: list[Any], since: float, until: float) -> dict:
    outer = _LOG_TIMESTAMP
    terminal = re.compile(
        r"(Completed|Failed) queue item .* \(([0-9a-f-]{36})\)"
        r"(?: in ([0-9.]+) seconds| after ([0-9.]+) seconds)",
        re.I,
    )
    play = re.compile(r"play-timing nzo=([0-9a-f-]{36}).*?firstSeg=(\d+)ms")
    durations = []
    first_segments = []
    completed = 0
    failed = 0
    bins: dict[int, int] = defaultdict(int)
    timings: dict[str, float] = {}
    for file_entry in files:
        if isinstance(file_entry, dict):
            file_name = file_entry.get("path")
            offset = max(0, int(file_entry.get("offset") or 0))
        else:
            file_name = file_entry
            offset = 0
        try:
            handle = open(file_name, "rb")
        except OSError:
            continue
        with handle:
            if offset:
                handle.seek(offset)
                handle.readline()
            for raw_line in handle:
                line = raw_line.decode("utf-8", errors="replace")
                match = outer.match(line)
                if not match:
                    continue
                try:
                    timestamp = (
                        datetime.strptime(match.group("stamp"), "%b %d, %Y %H:%M:%S")
                        .replace(tzinfo=timezone.utc)
                        .timestamp()
                    )
                except ValueError:
                    continue
                if timestamp < since or timestamp >= until:
                    continue
                if play_match := play.search(line):
                    timings[play_match.group(1)] = float(play_match.group(2))
                if terminal_match := terminal.search(line):
                    kind = terminal_match.group(1).lower()
                    completed += kind == "completed"
                    failed += kind == "failed"
                    duration = float(terminal_match.group(3) or terminal_match.group(4))
                    durations.append(duration)
                    bins[int(timestamp // 600)] += 1
                    if terminal_match.group(2) in timings:
                        first_segments.append(timings[terminal_match.group(2)])
    active_bins = [value for value in bins.values() if value >= 10]
    total = completed + failed
    return {
        "terminal_items": total,
        "completed": completed,
        "failed": failed,
        "failure_percent": round(failed * 100 / total, 2) if total else None,
        "duration_seconds": {
            "median": round(statistics.median(durations), 2) if durations else None,
            "p90": round(_percentile(durations, 0.9) or 0, 2) if durations else None,
            "maximum": round(max(durations), 2) if durations else None,
            "over_five_minutes": sum(value > 300 for value in durations),
        },
        "first_segment_ms": {
            "median": (
                round(statistics.median(first_segments), 2) if first_segments else None
            ),
            "p90": (
                round(_percentile(first_segments, 0.9) or 0, 2)
                if first_segments
                else None
            ),
        },
        "busy_period_items_per_hour": {
            "median": (
                round(statistics.median(active_bins) * 6, 2) if active_bins else None
            ),
            "average": (
                round(statistics.fmean(active_bins) * 6, 2) if active_bins else None
            ),
            "peak": max(active_bins) * 6 if active_bins else None,
            "ten_minute_bins": len(active_bins),
        },
    }


def collect_nzbdav_diagnostics(
    service_config: dict,
    *,
    windows: dict,
    log_scan: dict | None,
) -> dict:
    env = service_config.get("env") or {}
    configured = (
        env.get("CONFIG_PATH")
        or service_config.get("config_dir")
        or service_config.get("config_path")
        or "/nzbdav"
    )
    base = Path(str(configured))
    if base.suffix:
        base = base.parent
    result = {
        "collector": "nzbdav",
        "available": False,
        "config": _nzbdav_config(base),
        "limitations": [
            "NzbDAV metrics do not measure Plex click-to-first-frame latency.",
            "Observed changes are correlations unless the comparison boundary is a recorded configuration event.",
        ],
    }
    metrics_path = base / "metrics.sqlite"
    current_window = windows["current"]
    baseline_window = windows.get("baseline")
    if metrics_path.is_file():
        try:
            with _sqlite_connect_readonly(metrics_path) as connection:
                current = _nzbdav_metric_window(
                    connection,
                    current_window["since"],
                    current_window["until"],
                )
                result["metrics"] = {"current": current}
                if baseline_window:
                    baseline = _nzbdav_metric_window(
                        connection,
                        baseline_window["since"],
                        baseline_window["until"],
                    )
                    result["metrics"]["baseline"] = baseline
                    result["metrics"]["changes"] = _metric_window_changes(
                        current, baseline
                    )
                result["available"] = True
        except (OSError, sqlite3.Error) as exc:
            result["metrics_error"] = (
                f"NzbDAV metrics query failed: {type(exc).__name__}"
            )
    else:
        result["metrics_error"] = "NzbDAV metrics database not found."

    files = (log_scan or {}).get("_file_ranges") or (
        (log_scan or {}).get("_files") or []
    )
    if files:
        current = _nzbdav_log_window(
            files,
            current_window["since"],
            current_window["until"],
        )
        result["queue"] = {"current": current}
        if baseline_window:
            baseline = _nzbdav_log_window(
                files,
                baseline_window["since"],
                baseline_window["until"],
            )
            result["queue"]["baseline"] = baseline
            current_rate = (current.get("busy_period_items_per_hour") or {}).get(
                "median"
            )
            baseline_rate = (baseline.get("busy_period_items_per_hour") or {}).get(
                "median"
            )
            if current_rate is not None and baseline_rate not in (None, 0):
                result["queue"]["throughput_change_percent"] = round(
                    ((current_rate - baseline_rate) / baseline_rate) * 100,
                    2,
                )
        result["available"] = True
    return result


def collect_native_diagnostics(
    config_key: str | None,
    service_config: dict,
    *,
    windows: dict,
    log_scan: dict | None,
) -> dict:
    if str(config_key or "").lower() == "nzbdav":
        return collect_nzbdav_diagnostics(
            service_config,
            windows=windows,
            log_scan=log_scan,
        )
    return {
        "collector": str(config_key or "generic"),
        "available": False,
        "reason": "No native service collector is registered; generic diagnostics remain available.",
    }


def build_recommendation_context(evidence: dict) -> list[dict]:
    recommendations = []
    logs = evidence.get("logs") or {}
    levels = logs.get("levels") or {}
    if levels.get("error", 0) or levels.get("critical", 0):
        recommendations.append(
            {
                "id": "review-new-errors",
                "title": "Review recurring and newly introduced errors",
                "risk": "low",
                "action": "review",
                "reason": (
                    f"The retained-log scan found {levels.get('error', 0) + levels.get('critical', 0)} "
                    "error or critical entries in the selected window."
                ),
                "restart_required": False,
                "automatic_apply_supported": False,
            }
        )
    runtime = evidence.get("runtime_metrics") or {}
    cpu_change = (runtime.get("changes") or {}).get("cpu_percent_average_percent")
    if cpu_change is not None and cpu_change >= 25:
        recommendations.append(
            {
                "id": "investigate-cpu-increase",
                "title": "Investigate increased CPU usage",
                "risk": "low",
                "action": "observe",
                "reason": f"Average CPU usage increased by {cpu_change:.1f}% versus the baseline.",
                "restart_required": False,
                "automatic_apply_supported": False,
            }
        )
    native = evidence.get("native") or {}
    queue = native.get("queue") or {}
    current_queue = queue.get("current") or {}
    stalls = (current_queue.get("duration_seconds") or {}).get("over_five_minutes") or 0
    if stalls:
        recommendations.append(
            {
                "id": "investigate-nzbdav-processor-stalls",
                "title": "Investigate long-running NzbDAV queue processors",
                "risk": "low",
                "action": "review",
                "reason": f"{stalls} queue item(s) exceeded five minutes in the selected window.",
                "restart_required": False,
                "automatic_apply_supported": False,
            }
        )
    return recommendations


def strip_private_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_private_fields(child)
            for key, child in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [strip_private_fields(child) for child in value]
    return value
