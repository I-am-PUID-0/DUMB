import json
import psutil
import sys


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


def verify_processes(running_processes):
    error_messages = []
    for process_name, pid in running_processes.items():
        if not psutil.pid_exists(pid):
            error_messages.append(
                f"The process {process_name} (PID: {pid}) is not running."
            )
    return error_messages


def main():
    file_path = "/healthcheck/running_processes.json"
    running_processes = load_running_processes(file_path)
    errors = verify_processes(running_processes)

    if errors:
        print(" | ".join(errors), file=sys.stderr)
        sys.exit(1)
    else:
        print("All processes are healthy.")
        sys.exit(0)


if __name__ == "__main__":
    main()
