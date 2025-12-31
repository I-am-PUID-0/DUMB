import json
import os
import time


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
