import json
import psutil
import socket
import sys

try:
    from utils.config_loader import CONFIG_MANAGER
except Exception:
    CONFIG_MANAGER = None


def load_running_processes(file_path="/healthcheck/running_processes.json"):
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: Running processes file not found: {file_path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Error: Failed to decode JSON in {file_path}", file=sys.stderr)
        sys.exit(1)


def load_startup_state(file_path="/healthcheck/startup_state.json"):
    try:
        with open(file_path, encoding="utf-8") as handle:
            value = json.load(handle)
            return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _collect_config_ports(config):
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


def _normalize_host(host):
    if not host or host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _is_port_open(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _get_process_config(process_name):
    if not CONFIG_MANAGER:
        return None
    key, instance_name = CONFIG_MANAGER.find_key_for_process(process_name)
    if not key and not instance_name:
        return None
    return CONFIG_MANAGER.get_instance(instance_name, key)


def verify_processes(running_processes):
    error_messages = []
    for process_name, pid in running_processes.items():
        if process_name.lower() in {"plex dbrepair", "dbrepair"}:
            continue
        if not psutil.pid_exists(pid):
            error_messages.append(
                f"The process {process_name} (PID: {pid}) is not running."
            )
            continue

        try:
            proc = psutil.Process(pid)
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                error_messages.append(
                    f"The process {process_name} (PID: {pid}) is not healthy."
                )
                continue
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            error_messages.append(
                f"The process {process_name} (PID: {pid}) could not be inspected."
            )
            continue

        config = _get_process_config(process_name)
        if not config:
            continue

        host = _normalize_host(config.get("host"))
        ports = _collect_config_ports(config)
        for port in ports:
            if not _is_port_open(host, port):
                error_messages.append(
                    f"The process {process_name} (PID: {pid}) is not responding on {host}:{port}."
                )
    return error_messages


def main():
    file_path = "/healthcheck/running_processes.json"
    startup_state = load_startup_state()
    phase = startup_state.get("phase")
    if phase in {"initializing", "preinstalling", "starting_services", "stabilizing"}:
        print(f"DUMB startup is in progress ({phase}).")
        sys.exit(0)

    running_processes = load_running_processes(file_path)
    errors = verify_processes(running_processes)
    if phase == "degraded":
        failures = startup_state.get("failures") or {}
        if failures:
            errors.append("DUMB startup is degraded: " + ", ".join(sorted(failures)))
    elif phase == "shutting_down":
        errors.append("DUMB is shutting down.")

    if errors:
        print(" | ".join(errors), file=sys.stderr)
        sys.exit(1)
    else:
        print("All processes are healthy.")
        sys.exit(0)


if __name__ == "__main__":
    main()
