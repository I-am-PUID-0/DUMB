import os, socket, psutil
from json import load
from utils.config_loader import CONFIG_MANAGER


class APIState:
    def __init__(self, process_handler, logger):
        self.logger = logger
        self.process_handler = process_handler
        self.status_file_path = "/healthcheck/running_processes.json"
        os.makedirs(os.path.dirname(self.status_file_path), exist_ok=True)
        self._status_cache = {}
        self._status_mtime = None
        self.service_status = self._load_status_from_file()
        self._status_cache = self.service_status
        self._status_mtime = self._get_status_mtime()
        self.shutdown_in_progress = set()

    def _get_status_mtime(self):
        try:
            return os.path.getmtime(self.status_file_path)
        except FileNotFoundError:
            return None

    def _load_status_from_file(self):
        try:
            with open(self.status_file_path, "r") as f:
                data = load(f)
                return data
        except FileNotFoundError:
            self.logger.debug(
                f"Status file {self.status_file_path} not found. Initializing empty status."
            )
            with open(self.status_file_path, "w") as f:
                f.write("{}")
            return {}
        except Exception as e:
            self.logger.error(f"Error loading status file: {e}")
            return {}

    def _refresh_status_cache(self):
        mtime = self._get_status_mtime()
        if mtime is not None and mtime == self._status_mtime:
            return self._status_cache
        data = self._load_status_from_file()
        self._status_cache = data
        self._status_mtime = self._get_status_mtime()
        return data

    def get_status(self, process_name):
        running_processes = self._refresh_status_cache()

        def normalize(name):
            return name.replace(" ", "").replace("/ ", "/").strip().lower()

        normalized_input = normalize(process_name)
        if normalized_input == "dumbapi" or normalized_input == "dmbapi":
            return "running"
        for stored_name in running_processes:
            if normalized_input == normalize(stored_name):
                return "running"
        if normalized_input in {"plexdbrepair", "dbrepair"}:
            plex_cfg = CONFIG_MANAGER.get("plex", {}) or {}
            if plex_cfg.get("dbrepair", {}).get("enabled"):
                return "idle"
        return "stopped"

    def get_running_processes(self):
        running_processes = self._refresh_status_cache()
        if isinstance(running_processes, dict):
            return list(running_processes.keys())
        return []

    def get_status_details(self, process_name, include_health=False):
        running_processes = self._refresh_status_cache()

        def normalize(name):
            return name.replace(" ", "").replace("/ ", "/").strip().lower()

        normalized_input = normalize(process_name)
        status = "stopped"
        matched_name = None
        pid = None

        if normalized_input in ("dumbapi", "dmbapi"):
            status = "running"
        elif normalized_input in {"plexdbrepair", "dbrepair"}:
            plex_cfg = CONFIG_MANAGER.get("plex", {}) or {}
            if plex_cfg.get("dbrepair", {}).get("enabled"):
                status = "idle"
        else:
            for stored_name, stored_pid in running_processes.items():
                if normalized_input == normalize(stored_name):
                    status = "running"
                    matched_name = stored_name
                    pid = stored_pid
                    break

        if not include_health:
            return {"status": status}

        healthy, reason = self._check_health(matched_name, pid, status)
        restart_stats = self.process_handler.get_restart_stats(
            matched_name or process_name
        )
        return {
            "status": status,
            "healthy": healthy,
            "health_reason": reason,
            "restart": restart_stats,
        }

    def get_running_status_snapshot(self, include_health=False):
        running_processes = self._refresh_status_cache()
        if not isinstance(running_processes, dict):
            return []
        if not include_health:
            return list(running_processes.keys())
        snapshot = []
        for name, pid in running_processes.items():
            healthy, reason = self._check_health(name, pid, "running")
            snapshot.append(
                {
                    "process_name": name,
                    "status": "running",
                    "healthy": healthy,
                    "health_reason": reason,
                    "restart": self.process_handler.get_restart_stats(name),
                }
            )
        return snapshot

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

    def _normalize_host(self, host):
        if not host or host in {"0.0.0.0", "::"}:
            return "127.0.0.1"
        return host

    def _is_port_open(self, host, port, timeout=1.5):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def _get_process_config(self, process_name):
        if not CONFIG_MANAGER:
            return None
        key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
        if not key and not instance_name:
            return None
        return CONFIG_MANAGER.get_instance(instance_name, key)

    def _check_health(self, process_name, pid, status):
        if status == "idle":
            return True, "Process idle"
        if status != "running" or not process_name:
            return False, "Process not running"

        if not pid or not psutil.pid_exists(pid):
            return False, "Process PID not running"

        try:
            proc = psutil.Process(pid)
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return False, "Process not healthy"
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False, "Process could not be inspected"

        config = self._get_process_config(process_name)
        if not config:
            return True, None

        host = self._normalize_host(config.get("host"))
        ports = self._collect_config_ports(config)
        for port in ports:
            if not self._is_port_open(host, port):
                return False, f"Port {host}:{port} not responding"

        return True, None

    def debug_state(self):
        self.logger.info(f"Current APIState: {self.service_status}")
