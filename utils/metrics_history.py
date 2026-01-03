import json
import os
import time


class MetricsHistoryWriter:
    def __init__(
        self,
        base_dir,
        retention_days=7,
        max_file_mb=50,
        max_total_mb=100,
        logger=None,
    ):
        self.base_dir = base_dir
        self.retention_days = retention_days or 0
        self.max_file_bytes = int(max_file_mb * 1024 * 1024) if max_file_mb else 0
        self.max_total_bytes = int(max_total_mb * 1024 * 1024) if max_total_mb else 0
        self.logger = logger
        self.current_date = None
        self.current_index = 0
        self.current_path = None

        os.makedirs(self.base_dir, exist_ok=True)

    def write(self, snapshot):
        line = json.dumps(snapshot, separators=(",", ":"), ensure_ascii=True)
        path = self._ensure_current_file(len(line) + 1)
        try:
            with open(path, "a") as f:
                f.write(line + "\n")
        except Exception as exc:
            if self.logger:
                self.logger.error(f"Failed to write metrics history: {exc}")

        if self.retention_days > 0:
            self._prune_old_files()
        if self.max_total_bytes > 0:
            self._prune_total_size()

    def _ensure_current_file(self, line_size):
        today = time.strftime("%Y%m%d")
        if self.current_date != today:
            self.current_date = today
            self.current_index = self._find_latest_index(today)
            self.current_path = self._build_path(today, self.current_index)

        if self.max_file_bytes > 0:
            try:
                if os.path.exists(self.current_path):
                    current_size = os.path.getsize(self.current_path)
                else:
                    current_size = 0
                if current_size + line_size > self.max_file_bytes:
                    self.current_index += 1
                    self.current_path = self._build_path(today, self.current_index)
            except Exception:
                pass

        return self.current_path

    def _build_path(self, date_str, index):
        filename = f"metrics-{date_str}-{index:03d}.jsonl"
        return os.path.join(self.base_dir, filename)

    def _find_latest_index(self, date_str):
        prefix = f"metrics-{date_str}-"
        indexes = []
        for name in os.listdir(self.base_dir):
            if name.startswith(prefix) and name.endswith(".jsonl"):
                parts = name.replace(".jsonl", "").split("-")
                if len(parts) == 3 and parts[-1].isdigit():
                    indexes.append(int(parts[-1]))
        return max(indexes) if indexes else 0

    def _prune_old_files(self):
        cutoff = time.time() - (self.retention_days * 86400)
        for name in os.listdir(self.base_dir):
            if not name.startswith("metrics-") or not name.endswith(".jsonl"):
                continue
            path = os.path.join(self.base_dir, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except Exception:
                continue

    def _prune_total_size(self):
        files = []
        total_size = 0
        for name in os.listdir(self.base_dir):
            if not name.startswith("metrics-") or not name.endswith(".jsonl"):
                continue
            path = os.path.join(self.base_dir, name)
            try:
                size = os.path.getsize(path)
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            files.append((mtime, path, size))
            total_size += size

        if total_size <= self.max_total_bytes:
            return

        files.sort(key=lambda entry: entry[0])
        for _mtime, path, size in files:
            if total_size <= self.max_total_bytes:
                break
            try:
                os.remove(path)
                total_size -= size
            except Exception:
                continue
