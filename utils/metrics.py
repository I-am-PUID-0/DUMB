import os
import time
import psutil

from utils.database_health import DatabaseHealthCollector

DEFAULT_FILESYSTEM_PATHS = ["/"]
DEFAULT_NETWORK_INTERFACES = ["all"]
_IGNORED_FILESYSTEM_PREFIXES = ("/dev", "/proc", "/sys")
_IGNORED_FILESYSTEM_TYPES = {
    "autofs",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "proc",
    "pstore",
    "securityfs",
    "sysfs",
    "tracefs",
}
_NETWORK_COUNTER_FIELDS = (
    "sent_bytes",
    "recv_bytes",
    "sent_packets",
    "recv_packets",
    "errors_in",
    "errors_out",
    "drops_in",
    "drops_out",
)
_NETWORK_COUNTER_ATTRIBUTES = {
    "sent_bytes": "bytes_sent",
    "recv_bytes": "bytes_recv",
    "sent_packets": "packets_sent",
    "recv_packets": "packets_recv",
    "errors_in": "errin",
    "errors_out": "errout",
    "drops_in": "dropin",
    "drops_out": "dropout",
}


class MetricsCollector:
    def __init__(self, process_handler, logger):
        self.process_handler = process_handler
        self.logger = logger
        self._proc_cache = {}
        self.container_start_time = self._get_container_start_time()
        self._cgroup_last_cpu_usage = None
        self._cgroup_last_cpu_time = None
        self.database_health = DatabaseHealthCollector(logger=logger)

    def snapshot(
        self, external_limit=20, database_details=True, database_refresh=False
    ):
        from utils.config_loader import CONFIG_MANAGER

        now = time.time()
        managed = self._collect_managed_processes()
        managed_pids = {entry["pid"] for entry in managed if entry.get("pid")}
        external = self._collect_external_processes(managed_pids, limit=external_limit)
        return {
            "timestamp": now,
            "system": self._collect_system_metrics(),
            "dumb_managed": managed,
            "external": external,
            "database_health": self.database_health.snapshot(
                CONFIG_MANAGER.config,
                details=database_details,
                refresh_if_stale=database_refresh,
            ),
        }

    def _collect_system_metrics(self):
        from utils.config_loader import CONFIG_MANAGER

        metrics_config = CONFIG_MANAGER.get("dumb", {}).get("metrics", {})
        scope = metrics_config.get("system_scope", "host")
        filesystems = self._collect_filesystem_metrics(
            metrics_config.get("filesystem_paths")
        )
        net_io, network_interfaces = self._collect_network_metrics(
            metrics_config.get("network_interfaces")
        )
        effective_scope = scope
        if scope == "auto":
            effective_scope = "cgroup" if self._cgroup_available() else "host"
        if effective_scope == "cgroup":
            metrics = self._collect_system_metrics_cgroup(
                effective_scope, filesystems, net_io, network_interfaces
            )
            if metrics:
                return metrics
        return self._collect_system_metrics_host(
            effective_scope, filesystems, net_io, network_interfaces
        )

    def _collect_system_metrics_host(
        self, scope_label, filesystems=None, net_io=None, network_interfaces=None
    ):
        filesystems = filesystems or self._collect_filesystem_metrics()
        disk_usage, inode_usage = self._primary_filesystem_aliases(filesystems)
        disk_io = psutil.disk_io_counters()
        if net_io is None or network_interfaces is None:
            net_io, network_interfaces = self._collect_network_metrics()
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        load_avg = None
        try:
            load_avg = os.getloadavg()
        except (AttributeError, OSError):
            load_avg = None

        return {
            "scope": scope_label,
            "cpu_percent": psutil.cpu_percent(interval=None),
            "cpu_count": psutil.cpu_count(logical=True),
            "load_avg": list(load_avg) if load_avg else None,
            "mem": {
                "total": mem.total,
                "used": mem.used,
                "percent": mem.percent,
            },
            "swap": {
                "total": swap.total,
                "used": swap.used,
                "percent": swap.percent,
            },
            "disk": disk_usage,
            "inode": inode_usage,
            "filesystems": filesystems,
            "disk_io": {
                "read_bytes": disk_io.read_bytes if disk_io else 0,
                "write_bytes": disk_io.write_bytes if disk_io else 0,
                "read_count": disk_io.read_count if disk_io else 0,
                "write_count": disk_io.write_count if disk_io else 0,
            },
            "net_io": net_io,
            "network_interfaces": network_interfaces,
            "boot_time": psutil.boot_time(),
            "container_start_time": self.container_start_time,
        }

    def _collect_system_metrics_cgroup(
        self, scope_label, filesystems=None, net_io=None, network_interfaces=None
    ):
        cpu_limit = self._read_cgroup_cpu_limit()
        cpu_usage = self._read_cgroup_cpu_usage()
        if cpu_usage is None:
            return None

        now = time.time()
        cpu_percent = None
        if (
            self._cgroup_last_cpu_usage is not None
            and self._cgroup_last_cpu_time is not None
        ):
            delta_usage = cpu_usage - self._cgroup_last_cpu_usage
            delta_time = now - self._cgroup_last_cpu_time
            if delta_time > 0 and delta_usage >= 0 and cpu_limit > 0:
                cpu_percent = (delta_usage / delta_time) / cpu_limit * 100.0
        self._cgroup_last_cpu_usage = cpu_usage
        self._cgroup_last_cpu_time = now

        mem_current, mem_max = self._read_cgroup_memory()
        if mem_max is None or mem_max <= 0:
            mem = psutil.virtual_memory()
            mem_total = mem.total
            mem_used = mem.used
            mem_percent = mem.percent
        else:
            mem_total = mem_max
            mem_used = mem_current if mem_current is not None else 0
            mem_percent = (mem_used / mem_total * 100.0) if mem_total else None

        filesystems = filesystems or self._collect_filesystem_metrics()
        disk_usage, inode_usage = self._primary_filesystem_aliases(filesystems)
        disk_io = self._read_cgroup_io()
        if net_io is None or network_interfaces is None:
            net_io, network_interfaces = self._collect_network_metrics()
        swap = psutil.swap_memory()
        load_avg = None
        try:
            load_avg = os.getloadavg()
        except (AttributeError, OSError):
            load_avg = None

        return {
            "scope": scope_label,
            "cpu_percent": cpu_percent,
            "cpu_count": cpu_limit,
            "load_avg": list(load_avg) if load_avg else None,
            "mem": {
                "total": mem_total,
                "used": mem_used,
                "percent": mem_percent,
            },
            "swap": {
                "total": swap.total,
                "used": swap.used,
                "percent": swap.percent,
            },
            "disk": disk_usage,
            "inode": inode_usage,
            "filesystems": filesystems,
            "disk_io": {
                "read_bytes": disk_io.get("read_bytes", 0),
                "write_bytes": disk_io.get("write_bytes", 0),
                "read_count": 0,
                "write_count": 0,
            },
            "net_io": net_io,
            "network_interfaces": network_interfaces,
            "boot_time": psutil.boot_time(),
            "container_start_time": self.container_start_time,
        }

    def _collect_inode_usage(self, path):
        try:
            stats = os.statvfs(path)
            total = int(stats.f_files or 0)
            free = int(stats.f_ffree or 0)
            used = max(0, total - free)
            percent = (used / total * 100.0) if total else None
            return {
                "total": total,
                "used": used,
                "free": free,
                "percent": percent,
                "path": path,
            }
        except OSError:
            return {
                "total": None,
                "used": None,
                "free": None,
                "percent": None,
                "path": path,
            }

    def _collect_filesystem_metrics(self, configured_paths=None):
        paths = _normalize_filesystem_paths(configured_paths)
        mount_entries = _read_mount_entries()
        filesystems = []
        for path in paths:
            mount = _mount_for_path(path, mount_entries)
            entry = {
                "path": path,
                "mount_point": mount.get("mount_point") if mount else None,
                "fs_type": mount.get("fs_type") if mount else None,
                "available": False,
                "total": None,
                "used": None,
                "free": None,
                "percent": None,
                "inode": self._collect_inode_usage(path),
            }
            try:
                usage = psutil.disk_usage(path)
                entry.update(
                    {
                        "available": True,
                        "total": usage.total,
                        "used": usage.used,
                        "free": getattr(
                            usage, "free", max(usage.total - usage.used, 0)
                        ),
                        "percent": usage.percent,
                    }
                )
            except (FileNotFoundError, PermissionError, OSError):
                pass
            filesystems.append(entry)
        return filesystems

    def _primary_filesystem_aliases(self, filesystems):
        primary = (
            filesystems[0]
            if filesystems
            else {
                "path": "/",
                "mount_point": None,
                "fs_type": None,
                "available": False,
                "total": None,
                "used": None,
                "free": None,
                "percent": None,
                "inode": self._collect_inode_usage("/"),
            }
        )
        disk = {
            key: primary.get(key)
            for key in (
                "total",
                "used",
                "free",
                "percent",
                "path",
                "mount_point",
                "fs_type",
                "available",
            )
        }
        return disk, primary.get("inode") or self._collect_inode_usage(
            primary.get("path") or "/"
        )

    def list_filesystem_candidates(self):
        candidates = []
        seen = set()
        for mount in _read_mount_entries():
            path = mount["mount_point"]
            fs_type = mount.get("fs_type")
            if path in seen or not _is_filesystem_candidate(path, fs_type):
                continue
            metrics = self._collect_filesystem_metrics([path])[0]
            if not metrics.get("available"):
                continue
            seen.add(path)
            candidates.append(metrics)
        return sorted(
            candidates, key=lambda entry: (entry["path"] != "/", entry["path"])
        )

    def _collect_network_metrics(self, configured_interfaces=None):
        selected = _normalize_network_interfaces(configured_interfaces)
        try:
            counters = psutil.net_io_counters(pernic=True) or {}
        except (AttributeError, OSError):
            counters = {}
        try:
            stats = psutil.net_if_stats() or {}
        except (AttributeError, OSError):
            stats = {}

        names = sorted(counters)
        if selected != DEFAULT_NETWORK_INTERFACES:
            names = selected
        interfaces = []
        for name in names:
            counter = counters.get(name)
            interface_stats = stats.get(name)
            interface_speed = getattr(interface_stats, "speed", None)
            interfaces.append(
                {
                    "name": name,
                    "available": counter is not None,
                    "is_up": (bool(interface_stats.isup) if interface_stats else None),
                    "speed_mbps": (
                        int(interface_speed)
                        if interface_speed is not None and interface_speed >= 0
                        else None
                    ),
                    "mtu": (
                        int(interface_stats.mtu)
                        if interface_stats
                        and getattr(interface_stats, "mtu", None) is not None
                        else None
                    ),
                    **_network_counter_payload(counter),
                }
            )

        aggregate = {
            key: sum(
                int(interface.get(key) or 0)
                for interface in interfaces
                if interface.get("available")
            )
            for key in _NETWORK_COUNTER_FIELDS
        }
        return aggregate, interfaces

    def list_network_interface_candidates(self):
        _, interfaces = self._collect_network_metrics(DEFAULT_NETWORK_INTERFACES)
        return interfaces

    def _cgroup_available(self):
        return os.path.exists("/sys/fs/cgroup/cpu.stat")

    def _read_cgroup_cpu_usage(self):
        path = "/sys/fs/cgroup/cpu.stat"
        data = self._read_cgroup_key_values(path)
        usage_usec = data.get("usage_usec")
        if usage_usec is None:
            return None
        return usage_usec / 1_000_000.0

    def _read_cgroup_cpu_limit(self):
        path = "/sys/fs/cgroup/cpu.max"
        try:
            with open(path, "r") as f:
                content = f.read().strip().split()
        except OSError:
            content = []
        if len(content) >= 2 and content[0] != "max":
            try:
                quota = float(content[0])
                period = float(content[1])
                if quota > 0 and period > 0:
                    return max(quota / period, 0.1)
            except ValueError:
                pass
        return psutil.cpu_count(logical=True) or 1

    def _read_cgroup_memory(self):
        current = self._read_cgroup_int("/sys/fs/cgroup/memory.current")
        max_val = self._read_cgroup_int("/sys/fs/cgroup/memory.max", allow_max=True)
        if max_val == "max":
            max_val = None
        return current, max_val

    def _read_cgroup_io(self):
        path = "/sys/fs/cgroup/io.stat"
        read_bytes = 0
        write_bytes = 0
        try:
            with open(path, "r") as f:
                for line in f:
                    parts = line.strip().split()
                    for part in parts[1:]:
                        if part.startswith("rbytes="):
                            read_bytes += int(part.split("=", 1)[1])
                        elif part.startswith("wbytes="):
                            write_bytes += int(part.split("=", 1)[1])
        except OSError:
            return {}
        return {"read_bytes": read_bytes, "write_bytes": write_bytes}

    def _read_cgroup_int(self, path, allow_max=False):
        try:
            with open(path, "r") as f:
                value = f.read().strip()
        except OSError:
            return None
        if allow_max and value == "max":
            return "max"
        try:
            return int(value)
        except ValueError:
            return None

    def _read_cgroup_key_values(self, path):
        data = {}
        try:
            with open(path, "r") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) != 2:
                        continue
                    key, value = parts
                    try:
                        data[key] = int(value)
                    except ValueError:
                        continue
        except OSError:
            return {}
        return data

    def _collect_managed_processes(self):
        from utils.config_loader import CONFIG_MANAGER

        managed = []
        mount_paths = self._collect_mount_paths()
        for pid, info in list(self.process_handler.processes.items()):
            process_name = info.get("name")
            entry = {
                "name": process_name,
                "pid": pid,
                "start_time": info.get("start_time"),
            }
            proc = self._get_process(pid)
            if proc:
                entry.update(self._collect_process_metrics(proc))
            key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
            if key or instance_name:
                config = CONFIG_MANAGER.get_instance(instance_name, key)
                config_ports = self._collect_config_ports(config)
                entry["disk_paths"] = self._collect_disk_paths(
                    config, extra_paths=mount_paths
                )
                if config_ports:
                    entry["ports_config"] = config_ports
            managed.append(entry)
        return managed

    def _collect_external_processes(self, managed_pids, limit=20):
        candidates = []
        for proc in psutil.process_iter(["pid", "name"]):
            if proc.info["pid"] in managed_pids:
                continue
            metrics = {"name": proc.info.get("name"), "pid": proc.info.get("pid")}
            metrics.update(self._collect_process_metrics(proc))
            metrics["container_id"] = self._detect_container_id(proc.info.get("pid"))
            candidates.append(metrics)

        candidates.sort(key=lambda item: item.get("cpu_percent", 0.0), reverse=True)
        return candidates[:limit]

    def _collect_process_metrics(self, proc):
        metrics = {}
        try:
            metrics["cpu_percent"] = proc.cpu_percent(interval=None)
            mem = proc.memory_info()
            metrics["rss"] = mem.rss
            metrics["vms"] = mem.vms
            metrics["threads"] = proc.num_threads()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return metrics

        try:
            io_counters = proc.io_counters()
            metrics["disk_io"] = {
                "read_bytes": io_counters.read_bytes,
                "write_bytes": io_counters.write_bytes,
                "read_count": io_counters.read_count,
                "write_count": io_counters.write_count,
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            metrics["disk_io"] = None

        detected_ports = self._collect_listen_ports(proc)
        if detected_ports:
            metrics["ports"] = detected_ports
        connections = self._collect_process_connections(proc)
        if connections:
            metrics["net_connections"] = connections
        return metrics

    def _collect_listen_ports(self, proc):
        try:
            ports = set()
            for conn in proc.net_connections(kind="inet"):
                if conn.status == psutil.CONN_LISTEN and conn.laddr:
                    ports.add(conn.laddr.port)
            return sorted(ports)
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            return []

    def _collect_process_connections(self, proc, limit=50):
        try:
            connections = []
            for conn in proc.net_connections(kind="inet"):
                if conn.status != psutil.CONN_ESTABLISHED:
                    continue
                entry = {
                    "status": conn.status,
                    "laddr": _addr_to_tuple(conn.laddr),
                    "raddr": _addr_to_tuple(conn.raddr),
                }
                connections.append(entry)
                if limit and len(connections) >= limit:
                    break
            return connections
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            return []

    def _collect_config_ports(self, config):
        ports = set()
        for key in ("port", "frontend_port", "backend_port", "webdav_port"):
            value = config.get(key)
            if isinstance(value, int):
                ports.add(value)
        env = config.get("env", {})
        for key in ("PORT", "FRONTEND_PORT", "BACKEND_PORT", "WEBDAV_PORT"):
            value = env.get(key)
            if isinstance(value, str) and value.isdigit():
                ports.add(int(value))
        return sorted(ports)

    def _collect_disk_paths(self, config, extra_paths=None):
        paths = set()
        config_dir = config.get("config_dir")
        if config_dir:
            paths.add(config_dir)
        for key in ("config_file", "log_file"):
            value = config.get(key)
            if value:
                paths.add(os.path.dirname(value))
        env = config.get("env", {})
        for value in env.values():
            if isinstance(value, str) and value.startswith("/") and "{" not in value:
                paths.add(value)
        if extra_paths:
            paths.update(extra_paths)

        disk_entries = []
        total_used = 0
        for path in sorted(paths):
            path_exists = os.path.exists(path)
            usage = None
            if path_exists:
                try:
                    usage = psutil.disk_usage(path)
                    total_used += usage.used
                except (FileNotFoundError, PermissionError):
                    usage = None
            disk_entries.append(
                {
                    "path": path,
                    "exists": path_exists,
                    "usage": (
                        {
                            "total": usage.total,
                            "used": usage.used,
                            "percent": usage.percent,
                        }
                        if usage
                        else None
                    ),
                }
            )
        return {"paths": disk_entries, "used_total": total_used}

    def _collect_mount_paths(self):
        from utils.config_loader import CONFIG_MANAGER

        paths = set()
        data_root = CONFIG_MANAGER.get("data_root")
        if data_root:
            paths.add(data_root)

        rclone_instances = CONFIG_MANAGER.get("rclone", {}).get("instances", {})
        for instance in rclone_instances.values():
            mount_dir = instance.get("mount_dir")
            mount_name = instance.get("mount_name")
            if mount_dir:
                paths.add(mount_dir)
            if mount_dir and mount_name:
                paths.add(os.path.join(mount_dir, mount_name))
        return paths

    def _detect_container_id(self, pid):
        cgroup_path = f"/proc/{pid}/cgroup"
        try:
            with open(cgroup_path, "r") as f:
                for line in f:
                    if "docker" in line or "kubepods" in line:
                        parts = line.strip().split("/")
                        if parts:
                            return parts[-1]
        except (FileNotFoundError, PermissionError):
            return None
        return None

    def _get_process(self, pid):
        cached = self._proc_cache.get(pid)
        if cached and cached.is_running():
            return cached
        try:
            proc = psutil.Process(pid)
            self._proc_cache[pid] = proc
            return proc
        except psutil.NoSuchProcess:
            return None

    def _get_container_start_time(self):
        try:
            return psutil.Process(1).create_time()
        except Exception:
            return time.time()


def _addr_to_tuple(addr):
    if not addr:
        return None
    return [addr.ip, addr.port]


def _normalize_filesystem_paths(configured_paths):
    values = (
        configured_paths
        if isinstance(configured_paths, list) and configured_paths
        else DEFAULT_FILESYSTEM_PATHS
    )
    paths = []
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value.startswith("/"):
            continue
        normalized = os.path.normpath(value)
        if normalized not in paths:
            paths.append(normalized)
        if len(paths) >= 32:
            break
    return paths or list(DEFAULT_FILESYSTEM_PATHS)


def _normalize_network_interfaces(configured_interfaces):
    values = (
        configured_interfaces
        if isinstance(configured_interfaces, list) and configured_interfaces
        else DEFAULT_NETWORK_INTERFACES
    )
    names = []
    for value in values:
        if not isinstance(value, str):
            continue
        name = value.strip()
        if not name:
            continue
        if name.lower() == "all":
            return list(DEFAULT_NETWORK_INTERFACES)
        if name not in names:
            names.append(name)
        if len(names) >= 32:
            break
    return names or list(DEFAULT_NETWORK_INTERFACES)


def _network_counter_payload(counter):
    return {
        key: (int(getattr(counter, attribute, 0)) if counter is not None else None)
        for key, attribute in _NETWORK_COUNTER_ATTRIBUTES.items()
    }


def _read_mount_entries():
    entries = []
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as handle:
            for line in handle:
                left, separator, right = line.rstrip("\n").partition(" - ")
                if not separator:
                    continue
                left_fields = left.split()
                right_fields = right.split()
                if len(left_fields) < 5 or not right_fields:
                    continue
                entries.append(
                    {
                        "mount_point": _decode_mount_field(left_fields[4]),
                        "fs_type": _decode_mount_field(right_fields[0]),
                    }
                )
    except OSError:
        return [{"mount_point": "/", "fs_type": None}]
    return entries or [{"mount_point": "/", "fs_type": None}]


def _decode_mount_field(value):
    for encoded, decoded in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(encoded, decoded)
    return value


def _mount_for_path(path, mount_entries):
    resolved = os.path.realpath(path) if os.path.exists(path) else os.path.abspath(path)
    best = None
    for entry in mount_entries:
        mount_point = entry.get("mount_point") or "/"
        if resolved == mount_point or resolved.startswith(
            mount_point.rstrip("/") + "/"
        ):
            if best is None or len(mount_point) > len(best["mount_point"]):
                best = entry
    return best


def _is_filesystem_candidate(path, fs_type):
    if not path.startswith("/") or not os.path.isdir(path):
        return False
    if fs_type in _IGNORED_FILESYSTEM_TYPES:
        return False
    return not any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in _IGNORED_FILESYSTEM_PREFIXES
    )
