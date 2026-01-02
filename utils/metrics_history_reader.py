import json
import os
import time
import math


def read_history(history_dir, since=None, full=False, limit=5000, default_hours=6):
    if since is None and not full:
        since = time.time() - (default_hours * 60 * 60)

    items = []
    truncated = False
    files = _list_history_files(history_dir)
    for path in reversed(files):
        for entry in _read_history_file(path):
            timestamp = entry.get("timestamp")
            if timestamp is None:
                continue
            if since is not None and timestamp < since:
                continue
            items.append(entry)
            if limit > 0 and len(items) >= limit:
                truncated = True
                break
        if truncated:
            break

    items.sort(key=lambda item: item.get("timestamp", 0))
    return items, truncated


def _list_history_files(history_dir):
    if not os.path.isdir(history_dir):
        return []
    return sorted(
        [
            os.path.join(history_dir, name)
            for name in os.listdir(history_dir)
            if name.startswith("metrics-") and name.endswith(".jsonl")
        ]
    )


def _read_history_file(path):
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return


def _downsample_history_items(items, bucket_seconds=None, max_points=None):
    if not items:
        return []
    selected = items
    if bucket_seconds and bucket_seconds > 0:
        buckets = {}
        for entry in items:
            ts = entry.get("timestamp")
            if ts is None:
                continue
            bucket = int(ts // bucket_seconds)
            buckets[bucket] = entry
        selected = [buckets[key] for key in sorted(buckets.keys())]
    if max_points and max_points > 0 and len(selected) > max_points:
        step = int(math.ceil(len(selected) / max_points))
        full = selected
        selected = full[::step]
        if selected and full:
            if selected[-1].get("timestamp") != full[-1].get("timestamp"):
                selected.append(full[-1])
    return selected


def compact_history_items(items):
    compacted = []
    for item in items:
        system = item.get("system") or {}
        mem = system.get("mem") or {}
        disk = system.get("disk") or {}
        disk_io = system.get("disk_io") or {}
        net_io = system.get("net_io") or {}
        compacted.append(
            {
                "timestamp": item.get("timestamp"),
                "system": {
                    "cpu_percent": system.get("cpu_percent"),
                    "cpu_count": system.get("cpu_count"),
                    "mem": {"percent": mem.get("percent")} if mem else None,
                    "disk": {"percent": disk.get("percent")} if disk else None,
                    "disk_io": {
                        "read_bytes": disk_io.get("read_bytes"),
                        "write_bytes": disk_io.get("write_bytes"),
                    }
                    if disk_io
                    else None,
                    "net_io": {
                        "sent_bytes": net_io.get("sent_bytes"),
                        "recv_bytes": net_io.get("recv_bytes"),
                    }
                    if net_io
                    else None,
                },
                "dumb_managed": [
                    {
                        "name": proc.get("name"),
                        "pid": proc.get("pid"),
                        "cpu_percent": proc.get("cpu_percent"),
                        "rss": proc.get("rss"),
                        "disk_io": {
                            "read_bytes": (proc.get("disk_io") or {}).get("read_bytes"),
                            "write_bytes": (proc.get("disk_io") or {}).get("write_bytes"),
                        }
                        if proc.get("disk_io")
                        else None,
                    }
                    for proc in (item.get("dumb_managed") or [])
                ],
                "external": [
                    {
                        "name": proc.get("name"),
                        "pid": proc.get("pid"),
                        "cpu_percent": proc.get("cpu_percent"),
                        "rss": proc.get("rss"),
                        "disk_io": {
                            "read_bytes": (proc.get("disk_io") or {}).get("read_bytes"),
                            "write_bytes": (proc.get("disk_io") or {}).get("write_bytes"),
                        }
                        if proc.get("disk_io")
                        else None,
                    }
                    for proc in (item.get("external") or [])
                ],
            }
        )
    return compacted


def _build_rate_series(values, timestamps):
    series = []
    prev_value = None
    prev_ts = None
    for value, ts in zip(values, timestamps):
        if prev_value is None or value is None or ts is None or prev_ts is None:
            series.append(None)
        else:
            delta = value - prev_value
            dt = ts - prev_ts
            if dt <= 0 or delta < 0:
                series.append(None)
            else:
                series.append(delta / dt)
        prev_value = value
        prev_ts = ts
    return series


def build_history_series(items):
    timestamps = []
    cpu = []
    mem = []
    disk = []
    disk_read = []
    disk_write = []
    net_sent = []
    net_recv = []
    for item in items:
        timestamps.append(item.get("timestamp"))
        system = item.get("system") or {}
        cpu.append(system.get("cpu_percent"))
        mem.append((system.get("mem") or {}).get("percent"))
        disk.append((system.get("disk") or {}).get("percent"))
        disk_io = system.get("disk_io") or {}
        net_io = system.get("net_io") or {}
        disk_read.append(disk_io.get("read_bytes"))
        disk_write.append(disk_io.get("write_bytes"))
        net_sent.append(net_io.get("sent_bytes"))
        net_recv.append(net_io.get("recv_bytes"))

    return {
        "cpu": cpu,
        "mem": mem,
        "disk": disk,
        "disk_read_rate": _build_rate_series(disk_read, timestamps),
        "disk_write_rate": _build_rate_series(disk_write, timestamps),
        "net_sent_rate": _build_rate_series(net_sent, timestamps),
        "net_recv_rate": _build_rate_series(net_recv, timestamps),
    }


def _series_stats(values):
    series = [value for value in values if value is not None]
    if not series:
        return None
    return {"min": min(series), "max": max(series)}


def compute_history_stats(items):
    if not items:
        return None
    compacted = compact_history_items(items)
    series = build_history_series(compacted)
    return {
        "cpu": _series_stats(series.get("cpu", [])),
        "mem": _series_stats(series.get("mem", [])),
        "disk": _series_stats(series.get("disk", [])),
        "disk_read_rate": _series_stats(series.get("disk_read_rate", [])),
        "disk_write_rate": _series_stats(series.get("disk_write_rate", [])),
        "net_sent_rate": _series_stats(series.get("net_sent_rate", [])),
        "net_recv_rate": _series_stats(series.get("net_recv_rate", [])),
    }


def read_history_series(
    history_dir,
    since=None,
    full=False,
    limit=5000,
    default_hours=6,
    bucket_seconds=None,
    max_points=600,
):
    items, truncated = read_history(
        history_dir=history_dir,
        since=since,
        full=full,
        limit=limit,
        default_hours=default_hours,
    )
    selected = _downsample_history_items(
        items, bucket_seconds=bucket_seconds, max_points=max_points
    )
    compacted = compact_history_items(selected)
    series = build_history_series(compacted)
    stats = compute_history_stats(items)
    return compacted, series, truncated, stats
