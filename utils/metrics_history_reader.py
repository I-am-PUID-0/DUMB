import json
import os
import time
import math


def read_history(history_dir, since=None, full=False, limit=5000, default_hours=6):
    if since is None and not full:
        since = time.time() - (default_hours * 60 * 60)

    items = []
    truncated = False
    files = _list_history_files(history_dir, since=since)
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


def _list_history_files(history_dir, since=None):
    if not os.path.isdir(history_dir):
        return []
    cutoff_date = None
    if since is not None:
        try:
            cutoff_date = time.strftime("%Y%m%d", time.localtime(since))
        except (OSError, ValueError):
            cutoff_date = None
    files = []
    for name in os.listdir(history_dir):
        date_str = _parse_history_date(name)
        if not date_str:
            continue
        if cutoff_date and date_str < cutoff_date:
            continue
        files.append(os.path.join(history_dir, name))
    return sorted(files)


def _parse_history_date(name):
    if not name.startswith("metrics-") or not name.endswith(".jsonl"):
        return None
    parts = name.replace(".jsonl", "").split("-")
    if len(parts) != 3:
        return None
    date_str = parts[1]
    index_str = parts[2]
    if len(date_str) != 8 or not date_str.isdigit():
        return None
    if not index_str.isdigit():
        return None
    return date_str


def _read_history_file(path, on_decode_error=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as error:
                    if callable(on_decode_error):
                        on_decode_error(path, line_number, error)
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
        inode = system.get("inode") or {}
        filesystems = system.get("filesystems") or []
        disk_io = system.get("disk_io") or {}
        net_io = system.get("net_io") or {}
        network_interfaces = system.get("network_interfaces") or []
        compacted.append(
            {
                "timestamp": item.get("timestamp"),
                "system": {
                    "cpu_percent": system.get("cpu_percent"),
                    "cpu_count": system.get("cpu_count"),
                    "mem": {"percent": mem.get("percent")} if mem else None,
                    "disk": {"percent": disk.get("percent")} if disk else None,
                    "inode": {"percent": inode.get("percent")} if inode else None,
                    "filesystems": [
                        {
                            "path": filesystem.get("path"),
                            "mount_point": filesystem.get("mount_point"),
                            "fs_type": filesystem.get("fs_type"),
                            "available": filesystem.get("available"),
                            "percent": filesystem.get("percent"),
                            "inode": (
                                {
                                    "percent": (filesystem.get("inode") or {}).get(
                                        "percent"
                                    )
                                }
                                if filesystem.get("inode")
                                else None
                            ),
                        }
                        for filesystem in filesystems
                        if isinstance(filesystem, dict)
                    ],
                    "disk_io": (
                        {
                            "read_bytes": disk_io.get("read_bytes"),
                            "write_bytes": disk_io.get("write_bytes"),
                        }
                        if disk_io
                        else None
                    ),
                    "net_io": (
                        {
                            "sent_bytes": net_io.get("sent_bytes"),
                            "recv_bytes": net_io.get("recv_bytes"),
                        }
                        if net_io
                        else None
                    ),
                    "network_interfaces": [
                        {
                            "name": interface.get("name"),
                            "available": interface.get("available"),
                            "is_up": interface.get("is_up"),
                            "speed_mbps": interface.get("speed_mbps"),
                            "mtu": interface.get("mtu"),
                            "sent_bytes": interface.get("sent_bytes"),
                            "recv_bytes": interface.get("recv_bytes"),
                            "sent_packets": interface.get("sent_packets"),
                            "recv_packets": interface.get("recv_packets"),
                            "errors_in": interface.get("errors_in"),
                            "errors_out": interface.get("errors_out"),
                            "drops_in": interface.get("drops_in"),
                            "drops_out": interface.get("drops_out"),
                        }
                        for interface in network_interfaces
                        if isinstance(interface, dict)
                    ],
                },
                "dumb_managed": [
                    {
                        "name": proc.get("name"),
                        "pid": proc.get("pid"),
                        "cpu_percent": proc.get("cpu_percent"),
                        "rss": proc.get("rss"),
                        "disk_io": (
                            {
                                "read_bytes": (proc.get("disk_io") or {}).get(
                                    "read_bytes"
                                ),
                                "write_bytes": (proc.get("disk_io") or {}).get(
                                    "write_bytes"
                                ),
                            }
                            if proc.get("disk_io")
                            else None
                        ),
                    }
                    for proc in (item.get("dumb_managed") or [])
                ],
                "external": [
                    {
                        "name": proc.get("name"),
                        "pid": proc.get("pid"),
                        "cpu_percent": proc.get("cpu_percent"),
                        "rss": proc.get("rss"),
                        "disk_io": (
                            {
                                "read_bytes": (proc.get("disk_io") or {}).get(
                                    "read_bytes"
                                ),
                                "write_bytes": (proc.get("disk_io") or {}).get(
                                    "write_bytes"
                                ),
                            }
                            if proc.get("disk_io")
                            else None
                        ),
                    }
                    for proc in (item.get("external") or [])
                ],
                "database_health": item.get("database_health"),
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
    inode = []
    disk_read = []
    disk_write = []
    net_sent = []
    net_recv = []
    filesystem_paths = []
    for item in items:
        for filesystem in (item.get("system") or {}).get("filesystems") or []:
            path = filesystem.get("path") if isinstance(filesystem, dict) else None
            if path and path not in filesystem_paths:
                filesystem_paths.append(path)
    filesystems = {path: {"disk": [], "inode": []} for path in filesystem_paths}
    network_interface_names = []
    for item in items:
        for interface in (item.get("system") or {}).get("network_interfaces") or []:
            name = interface.get("name") if isinstance(interface, dict) else None
            if name and name not in network_interface_names:
                network_interface_names.append(name)
    network_interfaces = {
        name: {"sent": [], "recv": []} for name in network_interface_names
    }
    for item in items:
        timestamps.append(item.get("timestamp"))
        system = item.get("system") or {}
        cpu.append(system.get("cpu_percent"))
        mem.append((system.get("mem") or {}).get("percent"))
        disk.append((system.get("disk") or {}).get("percent"))
        inode.append((system.get("inode") or {}).get("percent"))
        disk_io = system.get("disk_io") or {}
        net_io = system.get("net_io") or {}
        disk_read.append(disk_io.get("read_bytes"))
        disk_write.append(disk_io.get("write_bytes"))
        net_sent.append(net_io.get("sent_bytes"))
        net_recv.append(net_io.get("recv_bytes"))
        filesystem_lookup = {
            filesystem.get("path"): filesystem
            for filesystem in system.get("filesystems") or []
            if isinstance(filesystem, dict) and filesystem.get("path")
        }
        for path in filesystem_paths:
            filesystem = filesystem_lookup.get(path) or {}
            filesystems[path]["disk"].append(filesystem.get("percent"))
            filesystems[path]["inode"].append(
                (filesystem.get("inode") or {}).get("percent")
            )
        interface_lookup = {
            interface.get("name"): interface
            for interface in system.get("network_interfaces") or []
            if isinstance(interface, dict) and interface.get("name")
        }
        for name in network_interface_names:
            interface = interface_lookup.get(name) or {}
            network_interfaces[name]["sent"].append(interface.get("sent_bytes"))
            network_interfaces[name]["recv"].append(interface.get("recv_bytes"))

    return {
        "cpu": cpu,
        "mem": mem,
        "disk": disk,
        "inode": inode,
        "disk_read_rate": _build_rate_series(disk_read, timestamps),
        "disk_write_rate": _build_rate_series(disk_write, timestamps),
        "net_sent_rate": _build_rate_series(net_sent, timestamps),
        "net_recv_rate": _build_rate_series(net_recv, timestamps),
        "filesystems": filesystems,
        "network_interfaces": {
            name: {
                "sent_rate": _build_rate_series(values["sent"], timestamps),
                "recv_rate": _build_rate_series(values["recv"], timestamps),
            }
            for name, values in network_interfaces.items()
        },
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
        "inode": _series_stats(series.get("inode", [])),
        "disk_read_rate": _series_stats(series.get("disk_read_rate", [])),
        "disk_write_rate": _series_stats(series.get("disk_write_rate", [])),
        "net_sent_rate": _series_stats(series.get("net_sent_rate", [])),
        "net_recv_rate": _series_stats(series.get("net_recv_rate", [])),
        "filesystems": {
            path: {
                "disk": _series_stats(values.get("disk", [])),
                "inode": _series_stats(values.get("inode", [])),
            }
            for path, values in (series.get("filesystems") or {}).items()
        },
        "network_interfaces": {
            name: {
                "sent_rate": _series_stats(values.get("sent_rate", [])),
                "recv_rate": _series_stats(values.get("recv_rate", [])),
            }
            for name, values in (series.get("network_interfaces") or {}).items()
        },
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
    return prepare_history_series(
        items,
        truncated=truncated,
        since=since,
        full=full,
        default_hours=default_hours,
        bucket_seconds=bucket_seconds,
        max_points=max_points,
    )


def prepare_history_series(
    items,
    truncated=False,
    since=None,
    full=False,
    default_hours=6,
    bucket_seconds=None,
    max_points=600,
):
    range_seconds = None
    if since is not None:
        range_seconds = max(time.time() - since, 1)
    elif not full:
        range_seconds = default_hours * 60 * 60

    if range_seconds is not None and max_points and max_points > 0:
        auto_bucket = max(5, int(math.ceil(range_seconds / max_points)))
        if bucket_seconds is None or bucket_seconds <= 0:
            bucket_seconds = auto_bucket
        else:
            bucket_seconds = max(bucket_seconds, auto_bucket)
    selected = _downsample_history_items(
        items, bucket_seconds=bucket_seconds, max_points=max_points
    )
    compacted = compact_history_items(selected)
    series = build_history_series(compacted)
    stats = compute_history_stats(items)
    return compacted, series, truncated, stats, bucket_seconds
